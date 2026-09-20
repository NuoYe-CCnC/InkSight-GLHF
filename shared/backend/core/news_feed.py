"""news_feed.py — 资讯三分类采集（第一阶段）

固定分类：综合新闻 / 科技AI / 财经（各取一条）。
源（可核验、稳定）：
  综合  央视新闻 news.cctv.com cmsdatainterface（title/url/focus_date/brief 原文给出）
  科技AI IT之家 RSS（按 AI 关键词优先挑最新符合项；源如实标注）
  财经  新浪财经·环球市场 roll lid=2516（title/intro/url/ctime）

规则：
  - 摘要只用来源自身提供的 brief/intro/description；没有就不给，绝不添加原文没有的信息。
  - 跨分类按规范化标题去重（保留先到/发布时间新者）。
  - 超长标题/摘要按上限截断并加省略号（URL 不截断）。
  - 每条含：分类/标题/简短摘要/来源名称/原文链接/发布时间/采集时间。
  - 失败保留上一内容（data_cache 组 news.<cat>），状态 stale/unknown。
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional

from . import data_cache as dc

logger = logging.getLogger(__name__)

CATEGORIES = ("general", "tech_ai", "finance")
CATEGORY_LABELS = {"general": "综合新闻", "tech_ai": "科技AI", "finance": "财经"}

TITLE_MAX = 60
SUMMARY_MAX = 120

# 科技/AI 命中关键词（标题），用于 IT之家流中挑 AI 类目条目
_AI_KEYWORDS = (
    "ai", "大模型", "模型", "openai", "gpt", "claude", "deepseek", "gemini",
    "智能", "芯片", "算力", "agent", "机器人", "自动驾驶", "神经网络", "kimi", "minimax",
    "英伟达", "nvidia", "微软", "谷歌", "苹果 ai", "华为", "苹果",
)

_SOURCES = {
    "general": {"name": "央视新闻"},
    "tech_ai": {"name": "IT之家"},
    "finance": {"name": "新浪财经·环球市场"},
}


def _now() -> float:
    return time.time()


def _clamp(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _norm_title(title: str) -> str:
    t = re.sub(r"\s+", "", title or "")
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", t)
    return t.lower()


# ── 各源拉取 ────────────────────────────────────────────────
def _fetch_cctv_general() -> Optional[dict]:
    url = "https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/news_1.jsonp?cb=t"
    raw = urllib.request.urlopen(url, timeout=15).read().decode("utf-8", "ignore")
    m = re.search(r"\((\{.*\})\)", raw, re.S)
    d = json.loads(m.group(1)) if m else json.loads(raw)
    items = (d.get("data") or {}).get("list") or d.get("list") or []
    if not items:
        return None
    it = items[0]
    title = str(it.get("title") or "").strip()
    if not title:
        return None
    return {
        "category": "general",
        "title": _clamp(title, TITLE_MAX),
        "summary": _clamp(it.get("brief") or "", SUMMARY_MAX),
        "source": _SOURCES["general"]["name"],
        "url": str(it.get("url") or "").strip(),
        "published_at": str(it.get("focus_date") or "").strip() or None,
        "collected_at": int(_now()),
        "_raw_key": _norm_title(title),
    }


def _fetch_ithome_tech() -> Optional[dict]:
    raw = urllib.request.urlopen("https://www.ithome.com/rss/", timeout=15).read()
    root = ET.fromstring(raw)
    channel = root.find("channel")
    items = list(channel.findall("item")) if channel is not None else []
    def pick(item):
        title = (item.findtext("title") or "").strip()
        if not title:
            return None
        return {
            "category": "tech_ai",
            "title": _clamp(title, TITLE_MAX),
            "summary": _clamp(item.findtext("description") or "", SUMMARY_MAX),
            "source": _SOURCES["tech_ai"]["name"],
            "url": str(item.findtext("link") or "").strip(),
            "published_at": str(item.findtext("pubDate") or "").strip() or None,
            "collected_at": int(_now()),
            "_raw_key": _norm_title(title),
        }
    # 优先 AI 关键词命中（自前向后第一条命中）；无命中则取最新一条
    for it in items:
        if any(k.lower() in (it.findtext("title") or "").lower() for k in _AI_KEYWORDS):
            return pick(it)
    return pick(items[0]) if items else None


def _fetch_sina_finance() -> Optional[dict]:
    url = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num=20&page=1"
    raw = urllib.request.urlopen(url, timeout=15).read()
    d = json.loads(raw.decode("utf-8", "ignore"))
    items = (d.get("result") or {}).get("data") or []
    if not items:
        return None
    it = items[0]
    title = str(it.get("title") or "").strip()
    if not title:
        return None
    return {
        "category": "finance",
        "title": _clamp(title, TITLE_MAX),
        "summary": _clamp(it.get("intro") or "", SUMMARY_MAX),
        "source": _SOURCES["finance"]["name"],
        "url": str(it.get("url") or "").strip(),
        "published_at": str(it.get("ctime") or "") or None,
        "collected_at": int(_now()),
        "_raw_key": _norm_title(title),
    }


_FETCHERS = {"general": _fetch_cctv_general, "tech_ai": _fetch_ithome_tech, "finance": _fetch_sina_finance}


# ── 去重（跨分类）与入库 ─────────────────────────────────────
def _seen_keys() -> set:
    g = dc.get_group("news.dedup") or {}
    return set(((g.get("value") or {}).get("seen") or {}).keys())


def _remember_key(raw_key: str, limit: int = 200) -> None:
    g = dc.get_group("news.dedup") or {}
    seen = dict(((g.get("value") or {}).get("seen") or {}))
    seen[raw_key] = int(_now())
    if len(seen) > limit:
        # 淘汰最旧
        for k in sorted(seen, key=seen.get)[: len(seen) - limit]:
            seen.pop(k, None)
    dc.record_success("news.dedup", {"seen": seen}, meta={"note": "title dedup ring"})


def refresh_category(cat: str, allow_dup: bool = False) -> dict:
    """拉取并入库一个分类。返回 {ok, item, status, skipped_dup}。"""
    fetch = _FETCHERS.get(cat)
    if fetch is None:
        return {"ok": False, "status": dc.group_status(f"news.{cat}"), "error": "unknown category"}
    group = f"news.{cat}"
    try:
        item = fetch()
    except Exception as e:  # noqa: BLE001
        logger.warning("news %s fetch error: %s", cat, e)
        st = dc.record_failure(group, note=str(e)[:120])
        return {"ok": False, "status": st, "error": str(e)[:120]}
    if item is None:
        st = dc.record_failure(group, note="empty feed")
        return {"ok": False, "status": st, "error": "empty feed"}
    # 兜底：长度与字段白名单在入库前再约束一次（adapter 失控也不越界）
    item["title"] = _clamp(item.get("title") or "", TITLE_MAX)
    item["summary"] = _clamp(item.get("summary") or "", SUMMARY_MAX)
    item = {k: v for k, v in item.items()
            if k in ("category", "title", "summary", "source", "url",
                     "published_at", "collected_at", "_raw_key")}
    raw_key = item.pop("_raw_key", None) or _norm_title(item.get("title", ""))
    if raw_key in _seen_keys() and not allow_dup:
        # 与其它分类重复：丢弃本条，保留本分类旧内容，置 fresh（内容未变）
        return {"ok": False, "skipped_dup": True,
                "status": dc.group_status(group), "item": dc.get_group(group)}
    _remember_key(raw_key)
    public = {k: v for k, v in item.items() if k != "_raw_key"}
    dc.record_success(group, public, meta={"source": public.get("source")})
    return {"ok": True, "item": public, "status": "fresh"}


def refresh_all() -> dict:
    """三分类各刷新一次（AI 面板用，节流在调用方）。"""
    out = {}
    for cat in CATEGORIES:
        out[cat] = refresh_category(cat)
    return out


def items_cached() -> dict:
    """内容层读取：读缓存不触发网络。返回 {cat: item|None}。"""
    out = {}
    for cat in CATEGORIES:
        g = dc.get_group(f"news.{cat}") or {}
        item = g.get("value")
        out[cat] = item if isinstance(item, dict) else None
    return out
