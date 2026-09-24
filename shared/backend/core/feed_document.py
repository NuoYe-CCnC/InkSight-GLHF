"""feed_document.py — 统一版本化数据文档（第一阶段）

将各可靠采集组（AI/新闻/金价）汇总为单一“关键数据文档”，
并维护分版本：ai_key（只随用量/余额/重置/会员变化）、news、gold、新鲜度。

用途：发布端/面板的“可靠数据层”产物；AI 设备的 <MAC>.json 仍按原 persona
流水线生成（本模块不改变其结构与 ts 语义），此处文档供未来 NEWS/GOLD 面板
及调试/审计使用。金价未确认标的 → 文档中 gold 明示 pending，绝不含报价。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from . import data_cache as dc
from . import gold_feed
from . import news_feed
from . import reliable_sources as rs
from .activity_signals import stable_minute_epoch

logger = logging.getLogger(__name__)

FEED_DIR = Path(__file__).resolve().parent.parent / "runtime_uploads"
FEED_FILE = FEED_DIR / "unified_feed.json"


def _atomic_write(path: Path, payload: dict) -> None:
    import os
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".feed.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _codex_current() -> dict:
    g = dc.get_group("ai.codex") or {}
    v = g.get("value")
    return v if isinstance(v, dict) else {}


def _ensure_codex_cached() -> None:
    """内容层可能已把 7D 结果写入；此处兜底：无缓存时不伪造。"""


def refresh_ai_key_version() -> str:
    """按当前缓存值刷新 ai_key 版本（内容不变则版本不变）。"""
    codex = _codex_current()
    # Codex 7D/重置 尽量从 content 层已落库组读（若还没有，来自真实 probe 则写入）
    seed = {
        "codex_7d_used": codex.get("used_percent"),
        # The panels display only HH:MM. Ignore provider samples that alternate
        # by one second for the same reset window so transport versions remain
        # stable as well as the device activity fingerprint.
        "codex_resets_at": stable_minute_epoch(codex.get("resets_at")),
        "codex_reset_credits": codex.get("reset_credits_available"),
        "codex_reset_expiry_list": codex.get("reset_expiry_list"),  # 变化必须更新 AI 版本
        "ds": rs.ds_values_cached(),
        "member": rs.member_config(),
        "deepseek_today": rs._deepseek_today(),
    }
    return dc.bump_version("ai_key", seed)


def refresh_news_version() -> Optional[str]:
    items = news_feed.items_cached()
    if not items or not any(items.values()):
        return dc.version("news")
    return dc.bump_version("news", items)


def refresh_gold_version() -> Optional[str]:
    item = gold_feed.cached()
    if item is None:
        return dc.version("gold")
    return dc.bump_version("gold", gold_feed.visual_seed(item))


def _expiry_list(codex: dict):
    """到期列表：缓存 api 列表优先；None 时回退手动配置（manual）；都无 → None。"""
    v = codex.get("reset_expiry_list")
    if isinstance(v, list) and codex.get("reset_expiry_source") == "api":
        return list(v)
    try:
        from .manual_reset_config import load_manual_reset
        m = load_manual_reset()
        if m and isinstance(m.get("expiry_list"), list) and m["expiry_list"]:
            import time as _t
            now_i = int(_t.time())
            ok = [int(x) for x in m["expiry_list"] if isinstance(x, (int, float)) and int(x) > now_i]
            if ok:
                return sorted(set(ok))
    except Exception:  # noqa: BLE001
        logger.debug("[FEED] manual reset expiry load skipped")
    return v if isinstance(v, list) else None


def _expiry_source(codex: dict) -> Optional[str]:
    v = codex.get("reset_expiry_source")
    if isinstance(v, str):
        return v
    try:
        from .manual_reset_config import load_manual_reset
        m = load_manual_reset()
        if m and isinstance(m.get("expiry_list"), list) and m["expiry_list"]:
            return "manual"
    except Exception:  # noqa: BLE001
        pass
    return None


def build_feed_document(write_file: bool = True) -> dict:
    """汇总统一文档（versioned）。写入本地时原子替换。"""
    now = int(time.time())
    ai_key_ver = refresh_ai_key_version()
    news_ver = refresh_news_version()
    gold_ver = refresh_gold_version()
    ds = rs.ds_values_cached()
    codex = _codex_current()
    member = rs.member_config()
    deepseek_today = rs._deepseek_today()
    gold_item = gold_feed.cached()
    try:
        from .openai_costs import snapshot as _openai_cost_snapshot
        openai_cost = _openai_cost_snapshot()
    except Exception:  # noqa: BLE001
        openai_cost = {"complete": False, "amount": None, "state": "unavailable"}
    feed = {
        "schema": "unified_feed_v1",
        "generated_at": now,
        "versions": {
            "ai_key": ai_key_ver,
            "news": news_ver,
            "gold": gold_ver,
        },
        "freshness": {
            "ai": dc.group_status("ai.codex") or dc.group_status("ai.ds.cny"),
            "news": {c: dc.group_status(f"news.{c}") for c in news_feed.CATEGORIES},
            "gold": dc.group_status(gold_feed.GROUP),
        },
        "ai": {
            "codex": {
                "seven_day": codex or None,
                "reset_credits_available": codex.get("reset_credits_available")
                if codex else None,
                # 手动重置到期列表（Unix 秒，升序未过期；None=未接通，[]=明确0次）
                # API(rateLimitResetCredits.credits[].expiresAt)优先；缺失回退手动配置(manual)
                "reset_expiry_list": _expiry_list(codex),
                "reset_expiry_source": _expiry_source(codex),
                "credit_balance": codex.get("credit_balance"),
                "credit_has_credits": codex.get("credit_has_credits"),
                "credit_unlimited": codex.get("credit_unlimited"),
                "account_key": codex.get("account_key"),
                "plan_expires_date": (member or {}).get("valid_until_date"),  # YYYY-MM-DD（precision=date）
                "unknown_reason": None,
            } if codex else {"seven_day": None, "unknown_reason": "no 7D window or no data"},
            "deepseek": {"CNY": ds.get("CNY"), "USD": ds.get("USD"),
                         "status": dc.group_status("ai.ds.cny")},
            "deepseek_today_tokens": deepseek_today.get("tokens"),
            "deepseek_today_tokens_complete": deepseek_today.get("complete", False),
            "deepseek_today_tokens_source": deepseek_today.get("source"),
            "member": member,
            "openai_api_month_spend": {
                "amount": openai_cost.get("amount") if openai_cost.get("complete") else None,
                "currency": "USD",
                "month": openai_cost.get("month"),
                "basis": "UTC",
                "state": openai_cost.get("state"),
                "complete": openai_cost.get("complete") is True,
            },
        },
        "news": {c: news_feed.items_cached().get(c) for c in news_feed.CATEGORIES},
        "gold": {"instrument": gold_feed.INSTRUMENT.get("instrument"),
                 "provider": gold_feed.PROVIDER, "unit": gold_feed.INSTRUMENT.get("unit"),
                 "channel_pending": gold_item is None,
                 "note": "尚无有效 XAUS 报价" if gold_item is None else None,
                 "item": gold_item},
    }
    if write_file:
        _atomic_write(FEED_FILE, feed)
    return feed


def current_feed() -> dict:
    try:
        return json.loads(FEED_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
