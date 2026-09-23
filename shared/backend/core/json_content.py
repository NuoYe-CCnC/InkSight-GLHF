"""
通用 JSON 模式内容生成器
根据 JSON content 定义调用 LLM 或返回静态数据
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import random
import re
import time
from json import JSONDecodeError
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import os

import httpx
from alibabacloud_alimt20181012 import models as alimt_models
from alibabacloud_alimt20181012.client import Client as AlimtClient
from alibabacloud_tea_openapi import models as open_api_models
from httpx import HTTPStatusError
from openai import OpenAIError

from .config import DEFAULT_LLM_PROVIDER, DEFAULT_LLM_MODEL, DEFAULT_IMAGE_PROVIDER, DEFAULT_IMAGE_MODEL
from .content import _build_context_str, _build_style_instructions, _call_llm, _clean_json_response
from .db import get_cache_db
from .errors import LLMKeyMissingError
from .layout_presets import expand_layout_presets

logger = logging.getLogger(__name__)

# Bump whenever firmware-side geometry changes and an identical content payload
# must be redrawn once after upgrade.
AI_USAGE_LAYOUT_VERSION = 9

# DeepSeek 余额 last-good 缓存（{mac: (balance, currency)}，接口抖动时兜底）
_AI_USAGE_LAST_DS: dict = {}

# Experiment switches
DISABLE_FALLBACK = os.environ.get("INKSIGHT_DISABLE_FALLBACK", "").strip().lower() in ("1", "true", "yes")
DISABLE_DEDUP = os.environ.get("INKSIGHT_DISABLE_DEDUP", "").strip().lower() in ("1", "true", "yes")

if DISABLE_FALLBACK:
    logger.warning("[EXP] Fallback is DISABLED via INKSIGHT_DISABLE_FALLBACK")
if DISABLE_DEDUP:
    logger.warning("[EXP] Deduplication is DISABLED via INKSIGHT_DISABLE_DEDUP")

DEDUP_MAX_RETRIES = 2

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_UPLOAD_DIR = _BACKEND_ROOT / "runtime_uploads"
_ALIYUN_MT_ENDPOINT = os.environ.get("ALIYUN_MT_ENDPOINT", "mt.cn-hangzhou.aliyuncs.com").strip()
_TIANAPI_ALMANAC_URL = "https://apis.tianapi.com/lunar/index"
_ALMANAC_FETCH_LOCK = asyncio.Lock()
_ALMANAC_CACHE_VERSION = 5
_MONTH_NAME_TO_NUM = {
    "一月": 1, "二月": 2, "三月": 3, "四月": 4, "五月": 5, "六月": 6,
    "七月": 7, "八月": 8, "九月": 9, "十月": 10, "十一月": 11, "十二月": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _resolve_uploaded_image_bytes(url: str) -> bytes | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    path = parsed.path or ""
    if not path.startswith("/api/uploads/"):
        return None
    upload_id = path.rsplit("/", 1)[-1].strip()
    if not upload_id:
        return None
    try:
        __import__("uuid").UUID(upload_id)
    except ValueError:
        return None
    file_path = _UPLOAD_DIR / f"{upload_id}.bin"
    if not file_path.exists() or not file_path.is_file():
        return None
    try:
        return file_path.read_bytes()
    except OSError:
        return None


def _is_uploaded_image_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (parsed.path or "").startswith("/api/uploads/")


def _has_cjk_text(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def _resolve_month_number(month_name: Any) -> int:
    key = str(month_name or "").strip().lower()
    return _MONTH_NAME_TO_NUM.get(key, 0)


def _resolve_almanac_date(date_ctx: dict | None = None) -> str:
    if isinstance(date_ctx, dict):
        try:
            year = int(date_ctx.get("year") or 0)
            day = int(date_ctx.get("day") or 0)
            month = _resolve_month_number(date_ctx.get("month_cn"))
            if year and month and day:
                return f"{year:04d}-{month:02d}-{day:02d}"
        except (TypeError, ValueError):
            pass
    return datetime.now().strftime("%Y-%m-%d")


def _normalize_lunar_day_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.replace("廿", "二十").replace("卅", "三十")


def _normalize_lunar_display(value: Any) -> str:
    text = str(value or "").strip().removeprefix("农历")
    if not text:
        return ""
    text = re.sub(r"^[〇零一二三四五六七八九十廿卅]+年", "", text)
    if "月" not in text:
        return _normalize_lunar_day_text(text)
    month, day = text.split("月", 1)
    return f"{month}月{_normalize_lunar_day_text(day)}"


def _normalize_almanac_list(value: Any, limit: int | None = None) -> str:
    text = str(value or "").strip().strip(".,，。、 ")
    if not text:
        return ""
    parts = [
        part.strip()
        for part in text.replace("，", ".").replace("、", ".").replace(",", ".").split(".")
        if part.strip()
    ]
    if limit is not None:
        parts = parts[:limit]
    return "、".join(parts)


def _normalize_almanac_shenwei(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return " ".join(part.strip() for part in re.split(r"\s+", text) if part.strip())


def _normalize_almanac_compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def _normalize_almanac_chongsha(value: Any) -> str:
    text = _normalize_almanac_compact(value)
    return text.replace("(", "（").replace(")", "）")


def _rule_based_almanac_health_tip(result: dict[str, Any], fallback_tip: str) -> str:
    yi = _normalize_almanac_list(result.get("fitness"), limit=3)
    ji = _normalize_almanac_list(result.get("taboo"), limit=3)
    solar_term = str(result.get("jieqi") or "").strip()

    def has_any(text: str, words: tuple[str, ...]) -> bool:
        return any(word in text for word in words)

    if has_any(ji, ("动土", "修造", "破土", "安葬")):
        return "少动土木，保持安稳"
    if has_any(ji, ("出行", "远行", "赴任")):
        return "行程从简，早些休息"
    if has_any(ji, ("嫁娶", "订盟", "纳采")):
        return "人情事务，宜缓不急"
    if has_any(yi, ("沐浴", "扫舍", "除服")):
        return "整理清洁，身心轻快"
    if has_any(yi, ("祭祀", "祈福", "会友")):
        return "静心处事，少些匆忙"
    if solar_term in ("小暑", "大暑", "立夏", "夏至"):
        return "暑热时节，清淡补水"
    if solar_term in ("立冬", "小雪", "大雪", "冬至", "小寒", "大寒"):
        return "寒时护暖，早睡养神"
    if solar_term in ("立春", "雨水", "惊蛰", "春分", "清明", "谷雨"):
        return "顺应春生，舒展身心"
    if solar_term in ("立秋", "处暑", "白露", "秋分", "寒露", "霜降"):
        return "秋燥渐起，润养作息"
    return str(fallback_tip or "").strip() or "起居有常，饮食清淡"


def _summarize_almanac_payload(result: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    solar_term = str(result.get("jieqi") or "").strip() or str(fallback.get("solar_term") or "").strip()
    yi = _normalize_almanac_list(result.get("fitness"), limit=3) or str(fallback.get("yi") or "").strip()
    ji = _normalize_almanac_list(result.get("taboo"), limit=3) or str(fallback.get("ji") or "").strip()
    shenwei = _normalize_almanac_shenwei(result.get("shenwei")) or str(fallback.get("shenwei") or "").strip()
    lunar_date_display = (
        f"{str(result.get('lubarmonth') or '').strip()}{_normalize_lunar_day_text(result.get('lunarday'))}"
        or str(fallback.get("lunar_date_display") or "").strip()
    )
    xingsu = str(result.get("xingsu") or "").strip() or str(fallback.get("xingsu") or "").strip()
    suisha = _normalize_almanac_compact(result.get("suisha")) or str(fallback.get("suisha") or "").strip()
    chongsha = _normalize_almanac_chongsha(result.get("chongsha")) or str(fallback.get("chongsha") or "").strip()
    return {
        "cache_version": _ALMANAC_CACHE_VERSION,
        "solar_term": solar_term,
        "lunar_date_display": lunar_date_display,
        "yi": yi,
        "ji": ji,
        "shenwei": shenwei,
        "xingsu": xingsu,
        "suisha": suisha,
        "chongsha": chongsha,
    }


def _base_almanac_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key not in {"cache_version", "translations"}
    }


def _get_almanac_translation(payload: dict[str, Any], language: str) -> dict[str, Any] | None:
    translations = payload.get("translations")
    if not isinstance(translations, dict):
        return None
    translated = translations.get(language)
    if not isinstance(translated, dict):
        return None
    return dict(translated)


async def _get_cached_almanac_payload(cache_date: str) -> dict[str, Any] | None:
    db = await get_cache_db()
    try:
        cursor = await db.execute(
            "SELECT cache_date, payload FROM daily_almanac_cache ORDER BY created_at DESC LIMIT 1",
        )
        row = await cursor.fetchone()
        if not row:
            return None
        if str(row[0]) != cache_date:
            await db.execute("DELETE FROM daily_almanac_cache")
            await db.commit()
            return None
        payload = json.loads(row[1])
        if not isinstance(payload, dict):
            return None
        if int(payload.get("cache_version") or 0) != _ALMANAC_CACHE_VERSION:
            await db.execute("DELETE FROM daily_almanac_cache")
            await db.commit()
            return None
        return payload
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("[ALMANAC] Failed to decode cached payload for %s", cache_date, exc_info=True)
        return None
    finally:
        await db.close()


async def _save_cached_almanac_payload(cache_date: str, payload: dict[str, Any]) -> None:
    db = await get_cache_db()
    try:
        data = json.dumps(payload, ensure_ascii=False)
        now = datetime.now().isoformat()
        await db.execute("DELETE FROM daily_almanac_cache")
        await db.execute(
            "INSERT INTO daily_almanac_cache (cache_date, payload, created_at) VALUES (?, ?, ?)",
            (cache_date, data, now),
        )
        await db.commit()
    finally:
        await db.close()


async def _fetch_tianapi_almanac_payload(cache_date: str) -> dict[str, Any] | None:
    api_key = os.environ.get("TIANAPI_KEY", "").strip()
    if not api_key:
        logger.warning("[ALMANAC] Missing TIANAPI_KEY, using fallback")
        return None
    last_error: Exception | None = None
    for trust_env in (True, False):
        try:
            async with httpx.AsyncClient(timeout=8.0, trust_env=trust_env) as client:
                response = await client.get(
                    _TIANAPI_ALMANAC_URL,
                    params={"key": api_key, "date": cache_date},
                )
                response.raise_for_status()
            data = response.json()
            if int(data.get("code") or 0) != 200 or not isinstance(data.get("result"), dict):
                logger.warning("[ALMANAC] TianAPI returned error for %s: %s", cache_date, data)
                return None
            return data["result"]
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            last_error = exc
    logger.warning("[ALMANAC] Failed to fetch TianAPI almanac for %s: %s", cache_date, last_error)
    return None


async def _generate_almanac_health_tip(result: dict[str, Any], fallback_tip: str) -> str:
    prompt = (
        "你是一个言简意赅、温和专业的黄历生活助手。\n"
        f"节气：{str(result.get('jieqi') or '').strip()}\n"
        f"宜：{_normalize_almanac_list(result.get('fitness'), limit=3)}\n"
        f"忌：{_normalize_almanac_list(result.get('taboo'), limit=3)}\n"
        f"神位：{_normalize_almanac_shenwei(result.get('shenwei'))}\n"
        f"星宿：{str(result.get('xingsu') or '').strip()}\n"
        f"岁煞：{str(result.get('suisha') or '').strip()}\n"
        f"冲煞：{str(result.get('chongsha') or '').strip()}\n"
        f"彭祖：{str(result.get('pengzu') or '').strip()}\n"
        "请结合上述黄历信息，生成一句今日的 health_tip。\n"
        "要求：只输出一句中文；严格控制在12到20个字；偏向起居、饮食、节律或情绪建议；直接输出正文，绝对不要解释，不要加引号。"
    )
    try:
        text = await _call_llm(
            DEFAULT_LLM_PROVIDER,
            DEFAULT_LLM_MODEL,
            prompt,
            temperature=0.4,
            max_tokens=48,
        )
        cleaned = str(text or "").strip().strip("“”\"' ")
        cleaned = re.sub(r"\s+", "", cleaned)
        if cleaned:
            return cleaned[:20]
    except (LLMKeyMissingError, httpx.HTTPError, HTTPStatusError, OpenAIError, OSError, TypeError, ValueError):
        logger.warning("[ALMANAC] Failed to generate daily health tip", exc_info=True)
    return _rule_based_almanac_health_tip(result, fallback_tip)


def _make_aliyun_mt_client() -> AlimtClient | None:
    access_key_id = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID", "").strip()
    access_key_secret = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "").strip()
    if not access_key_id or not access_key_secret:
        return None
    config = open_api_models.Config(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        endpoint=_ALIYUN_MT_ENDPOINT,
    )
    return AlimtClient(config)


def _translate_texts_with_aliyun_sync(
    texts: list[str],
    source_language: str = "en",
    target_language: str = "zh",
) -> list[str] | None:
    client = _make_aliyun_mt_client()
    if client is None:
        return None
    translated: list[str] = []
    try:
        for text in texts:
            request = alimt_models.TranslateGeneralRequest(
                source_language=source_language,
                target_language=target_language,
                format_type="text",
                scene="general",
                source_text=text,
            )
            response = client.translate_general(request)
            body = getattr(response, "body", None)
            data = getattr(body, "data", None)
            translated_text = getattr(data, "translated", "") if data is not None else ""
            translated_text = str(translated_text or "").strip()
            if not translated_text:
                return None
            translated.append(translated_text)
        return translated
    except Exception as e:
        logger.warning("[JSONContent] Aliyun translation error: %s", e)
        return None


async def _translate_with_aliyun_mt(
    texts: list[str],
    *,
    source_language: str = "en",
    target_language: str = "zh",
) -> list[str] | None:
    if not texts:
        return []
    return await asyncio.to_thread(
        _translate_texts_with_aliyun_sync,
        texts,
        source_language,
        target_language,
    )


async def _translate_almanac_payload_to_en(
    payload: dict[str, Any],
    fallback: dict[str, Any],
) -> dict[str, Any] | None:
    base = _base_almanac_payload(payload)
    fields = (
        "solar_term",
        "lunar_date_display",
        "yi",
        "ji",
        "shenwei",
        "xingsu",
        "health_tip",
    )
    texts = [str(base.get(field) or "").strip() for field in fields]
    non_empty = [(field, text) for field, text in zip(fields, texts) if text]
    if not non_empty:
        return None
    translated = await _translate_with_aliyun_mt(
        [text for _, text in non_empty],
        source_language="zh",
        target_language="en",
    )
    if not translated or len(translated) != len(non_empty):
        return None
    result = dict(fallback)
    for (field, _), text in zip(non_empty, translated):
        cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
        if cleaned:
            result[field] = cleaned
    return result


async def _translate_briefing_result(result: dict, language: str) -> dict:
    if language != "zh":
        return result
    targets: list[tuple[str, int | None, str]] = []
    texts: list[str] = []
    hn_items = result.get("hn_items")
    if isinstance(hn_items, list):
        for idx, item in enumerate(hn_items):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            if title and not _has_cjk_text(title):
                targets.append(("hn_items", idx, "title"))
                texts.append(title)
    devto_items = result.get("devto_items")
    if isinstance(devto_items, list):
        for idx, item in enumerate(devto_items):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            if title and not _has_cjk_text(title):
                targets.append(("devto_items", idx, "title"))
                texts.append(title)
    ph_tagline = str(result.get("ph_tagline", "")).strip()
    if ph_tagline and not _has_cjk_text(ph_tagline):
        targets.append(("root", None, "ph_tagline"))
        texts.append(ph_tagline)
    if not texts:
        return result
    translated = await _translate_with_aliyun_mt(texts)
    if not translated:
        return result
    for (container, idx, field_name), text in zip(targets, translated):
        if container == "root":
            result[field_name] = text
            continue
        items = result.get(container)
        if isinstance(items, list) and idx is not None and 0 <= idx < len(items) and isinstance(items[idx], dict):
            items[idx][field_name] = text
    if isinstance(result.get("devto_items"), list) and result["devto_items"]:
        first = result["devto_items"][0]
        if isinstance(first, dict):
            result["devto_title"] = str(first.get("title", result.get("devto_title", "")))
    return result

def _collect_image_fields(blocks: Any, fields: set):
    """Recursively collect image field names from legacy blocks or component trees."""
    if isinstance(blocks, dict):
        block = blocks
        if block.get("type") == "image":
            fields.add(block.get("field", "image_url"))
        for child_key in ("children", "left", "right", "item"):
            children = block.get(child_key)
            if isinstance(children, (list, dict)):
                _collect_image_fields(children, fields)
        return
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, (list, dict)):
                _collect_image_fields(block, fields)


def _daily_history_line(content: dict, language: str) -> str:
    quote = str(content.get("quote", "") or "").strip()
    book_title = str(content.get("book_title", "") or "").strip()
    tip = str(content.get("tip", "") or "").strip()
    season_text = str(content.get("season_text", "") or "").strip()
    parts: list[str] = []
    if language == "en":
        if quote:
            parts.append(f'quote="{quote[:80]}"')
        if book_title:
            parts.append(f'book="{book_title[:60]}"')
        if tip:
            parts.append(f'tip="{tip[:80]}"')
        if season_text:
            parts.append(f'season="{season_text[:60]}"')
    else:
        if quote:
            parts.append(f"语录「{quote[:40]}」")
        if book_title:
            parts.append(f"书籍「{book_title[:30]}」")
        if tip:
            parts.append(f"提示「{tip[:40]}」")
        if season_text:
            parts.append(f"时令「{season_text[:24]}」")
    return " | ".join(parts)


def _build_daily_dedup_hint(entries: list[dict], language: str) -> str:
    lines: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content", {})
        if not isinstance(content, dict):
            continue
        line = _daily_history_line(content, language)
        if line:
            lines.append(line)
    if not lines:
        return ""
    if language == "en":
        return (
            "\nAvoid repeating these recent DAILY outputs. Do not reuse the same quote, the same book, "
            "or a very similar tip/theme:\n- "
            + "\n- ".join(lines[:4])
        )
    return (
        "\n请避免与这些最近的 DAILY 内容重复，不要复用相同或相近的语录、书籍、tip 或时令表达：\n- "
        + "\n- ".join(lines[:4])
    )


async def _prefetch_images(content: dict, mode_def: dict) -> dict:
    """Pre-fetch any image URLs referenced by the layout into content dict."""
    layout = expand_layout_presets(mode_def.get("layout", {}))
    body_blocks = layout.get("body", [])
    image_fields: set = set()
    _collect_image_fields(body_blocks, image_fields)

    if not image_fields:
        return content

    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        for field_name in image_fields:
            url = content.get(field_name)
            if url and isinstance(url, str) and url.startswith("http"):
                local_bytes = _resolve_uploaded_image_bytes(url)
                if local_bytes:
                    content[f"_prefetched_{field_name}"] = local_bytes
                    continue
                if _is_uploaded_image_url(url):
                    content[f"_invalid_{field_name}"] = "Image link expired"
                    logger.warning("[JSONContent] Uploaded image link expired for field %s: %s", field_name, url)
                    continue
                try:
                    resp = await client.get(url)
                    if resp.status_code < 400:
                        content[f"_prefetched_{field_name}"] = resp.content
                except httpx.HTTPError:
                    logger.warning("[JSONContent] Failed to prefetch image field %s", field_name, exc_info=True)
    return content


def _get_fallback(content_cfg: dict) -> dict:
    """Get fallback content, supporting both single fallback and fallback_pool."""
    pool = content_cfg.get("fallback_pool")
    if pool and isinstance(pool, list) and len(pool) > 0:
        return dict(random.choice(pool))
    return dict(content_cfg.get("fallback", {}))


def _compute_content_hash(result: dict) -> str:
    text = json.dumps(result, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(text.encode()).hexdigest()[:12]


def _is_api_key_error(e: Exception) -> bool:
    """Check if exception indicates API key is invalid/expired (401/403)."""
    if isinstance(e, HTTPStatusError):
        status_code = e.response.status_code if hasattr(e, 'response') and e.response else None
        return status_code in (401, 403)
    
    if isinstance(e, OpenAIError):
        error_message = str(e).lower()
        error_code = getattr(e, 'status_code', None) or getattr(e, 'code', None)
        if error_code in (401, 403):
            return True
        auth_keywords = ("401", "403", "unauthorized", "invalid", "authentication")
        return any(kw in error_message for kw in auth_keywords)
    
    return False


def _validate_content_quality(result: dict, schema: dict | None = None) -> bool:
    """Validate LLM output quality. Returns True if acceptable."""
    if not result:
        return False
    for key, val in result.items():
        if isinstance(val, str) and len(val) > 500:
            return False
    important_keys = [k for k in result if k in ("quote", "question", "body", "word", "event_title", "challenge", "name_cn", "text")]
    for k in important_keys:
        if not result.get(k):
            return False
    return True


async def generate_json_mode_content(
    mode_def: dict,
    *,
    config: dict | None = None,
    date_ctx: dict | None = None,
    date_str: str = "",
    weather_str: str = "",
    festival: str = "",
    daily_word: str = "",
    upcoming_holiday: str = "",
    days_until_holiday: int = 0,
    character_tones: list[str] | None = None,
    language: str | None = None,
    content_tone: str | None = None,
    llm_provider: str = "",
    llm_model: str = "",
    llm_base_url: str | None = None,
    image_provider: str = "",
    image_model: str = "",
    mac: str = "",
    screen_w: int = 400,
    screen_h: int = 300,
    api_key: str = "",
    image_api_key: str = "",
) -> dict:
    """Generate content for a JSON-defined mode.

    Supports content types:
    - static: returns static_data from the definition
    - llm: calls LLM with prompt template, parses output per output_format
    - llm_json: calls LLM, parses JSON response using output_schema
    - external_data: fetches data from built-in providers (HN/PH/Dev.to)
    - image_gen: generates image data payload (ARTWALL provider)
    - computed: computes content from config/date without LLM
    - composite: merges results from multiple nested content steps
    """
    content_cfg = mode_def.get("content", {})
    ctype = content_cfg.get("type", "static")
    fallback = _get_fallback(content_cfg)
    mode_id = str(mode_def.get("mode_id") or "").upper()

    # Preview-only overrides: backend/api/index.py may inject per-mode overrides into
    # config["mode_overrides"][MODE_ID]. We allow these overrides to fill/replace
    # generated content fields (e.g. custom quote text, image_url for photo modes).
    override = {}
    try:
        cfg = config or {}
        mo = cfg.get("mode_overrides", {})
        if isinstance(mo, dict) and mode_id:
            candidate = mo.get(mode_id, {})
            if isinstance(candidate, dict):
                override = candidate
    except Exception:
        override = {}

    common_args = dict(
        date_str=date_str,
        weather_str=weather_str,
        festival=festival,
        daily_word=daily_word,
        upcoming_holiday=upcoming_holiday,
        days_until_holiday=days_until_holiday,
        character_tones=character_tones,
        language=language,
        content_tone=content_tone,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_base_url=llm_base_url,
        image_provider=image_provider,
        image_model=image_model,
        config=config or {},
        date_ctx=date_ctx or {},
        mac=mac,
        api_key=api_key,
        image_api_key=image_api_key,
    )

    # If override explicitly provides content fields, short-circuit LLM for llm_json.
    if ctype == "llm_json" and isinstance(override, dict) and override:
        quote = override.get("quote")
        author = override.get("author")
        if isinstance(quote, str) and quote.strip():
            result = dict(fallback)
            result["quote"] = quote.strip()
            if isinstance(author, str) and author.strip():
                result["author"] = author.strip()
            result = await _prefetch_images(result, mode_def)
            return result

    if ctype == "static":
        content = dict(content_cfg.get("static_data", fallback))
        if isinstance(override, dict) and override:
            # Merge overrides into static content (preview-only).
            for k, v in override.items():
                if k in {"city", "llm_provider", "llm_model", "image_provider", "image_model"}:
                    continue
                content[k] = v
        # MY_ADAPTIVE multi-image cycling: pick next image from image_urls array
        if mode_id == "MY_ADAPTIVE" and isinstance(override, dict):
            image_urls = override.get("image_urls")
            if isinstance(image_urls, list) and image_urls:
                urls = [u for u in image_urls if isinstance(u, str) and u.strip()]
            elif content.get("image_url"):
                urls = [content["image_url"]]
            else:
                urls = []
            if urls:
                if mac and len(urls) > 1:
                    from .config_store import get_photo_frame_index, set_photo_frame_index
                    idx = await get_photo_frame_index(mac)
                    content["image_url"] = urls[idx % len(urls)]
                    await set_photo_frame_index(mac, (idx + 1) % len(urls))
                else:
                    content["image_url"] = urls[0]
        content = await _prefetch_images(content, mode_def)
        return content
    if ctype == "computed":
        content = await _generate_computed_content(mode_def, content_cfg, fallback, **common_args)
        if isinstance(override, dict) and override:
            for k, v in override.items():
                if k in {"city", "llm_provider", "llm_model", "image_provider", "image_model"}:
                    continue
                if mode_id == "COUNTDOWN" and k in {"events", "countdownEvents", "message"}:
                    continue
                if mode_id == "HABIT" and k in {"habitItems", "habits", "summary", "week_progress", "week_total"}:
                    continue
                content[k] = v
        content = await _prefetch_images(content, mode_def)
        return content
    if ctype == "external_data":
        content = await _generate_external_data_content(mode_def, content_cfg, fallback, **common_args)
        if isinstance(override, dict) and override:
            for k, v in override.items():
                if k in {"city", "llm_provider", "llm_model", "image_provider", "image_model"}:
                    continue
                content[k] = v
        content = await _prefetch_images(content, mode_def)
        return content
    if ctype == "image_gen":
        content = await _generate_image_gen_content(mode_def, content_cfg, fallback, **common_args)
        if isinstance(override, dict) and override:
            for k, v in override.items():
                if k in {"city", "llm_provider", "llm_model", "image_provider", "image_model"}:
                    continue
                content[k] = v
        content = await _prefetch_images(content, mode_def)
        return content
    if ctype == "composite":
        content = await _generate_composite_content(mode_def, content_cfg, fallback, **common_args)
        if isinstance(override, dict) and override:
            for k, v in override.items():
                if k in {"city", "llm_provider", "llm_model", "image_provider", "image_model"}:
                    continue
                content[k] = v
        content = await _prefetch_images(content, mode_def)
        return content

    provider = llm_provider or DEFAULT_LLM_PROVIDER
    model = llm_model or DEFAULT_LLM_MODEL
    temperature = content_cfg.get("temperature", 0.8)
    max_tokens = content_cfg.get("max_tokens")

    context = _build_context_str(
        date_str, weather_str, festival, daily_word,
        upcoming_holiday, days_until_holiday,
        language=language,
    )
    base_prompt = content_cfg.get("prompt_template", "").replace("{context}", context)

    style = _build_style_instructions(character_tones, language, content_tone)
    if style:
        base_prompt += style

    if screen_h < 200:
        if language == "en":
            base_prompt += "\nNote: Content will be displayed on a tiny screen (296×128px). Keep all text very short."
        else:
            base_prompt += "\n注意：内容将显示在极小屏幕上（296×128像素），所有文字请尽量简短。"

    mode_id = mode_def.get("mode_id", "CUSTOM")
    logger.info(f"[JSONContent] Generating content for {mode_id} via {provider}/{model}")

    # Load recent content hashes for dedup
    recent_hashes: list[str] = []
    dedup_hint = ""
    first_attempt_hint = ""
    if mac and ctype in ("llm", "llm_json") and not DISABLE_DEDUP:
        try:
            from .stats_store import get_content_history, get_recent_content_hashes, get_recent_content_summaries
            recent_hashes = await get_recent_content_hashes(mac, mode_id, limit=20)
            summaries = await get_recent_content_summaries(mac, mode_id, limit=3)
            if summaries:
                if language == "en":
                    dedup_hint = "\nAvoid repeating these recent topics: " + "; ".join(summaries)
                else:
                    dedup_hint = "\n请避免与以下近期内容重复：" + "；".join(summaries)
            if mode_id == "DAILY":
                recent_entries = await get_content_history(mac, limit=4, mode=mode_id)
                first_attempt_hint = _build_daily_dedup_hint(recent_entries, language)
        except (OSError, TypeError, ValueError):
            logger.warning("[JSONContent] Failed to load dedup context for %s:%s", mac, mode_id, exc_info=True)

    for attempt in range(1 + DEDUP_MAX_RETRIES):
        prompt = base_prompt
        if first_attempt_hint:
            prompt += first_attempt_hint
        if attempt > 0 and dedup_hint:
            prompt += dedup_hint

        llm_ok = False
        api_key_invalid = False
        try:
            text = await _call_llm(
                provider,
                model,
                prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                api_key=api_key,
                base_url=llm_base_url,
            )
            llm_ok = True
        except (LLMKeyMissingError, httpx.HTTPError, HTTPStatusError, OpenAIError, OSError, TypeError, ValueError) as e:
            # 这里捕获所有 LLM 调用异常（包括 OpenAI/DeepSeek 的 BadRequestError 等），
            # 避免将 4xx/5xx 直接抛到上层导致 500，而是统一回退到 fallback 内容。
            logger.error(f"[JSONContent] LLM call failed for {mode_id}: {e}")
            if DISABLE_FALLBACK:
                result = {"text": f"[LLM_ERROR] {e}", "_is_fallback": True, "_llm_used": True, "_llm_ok": False}
                return _apply_post_process(result, content_cfg)
            # 检查是否是 API key 缺失或无效错误（401/403 等），用于给上游返回更明确的 api_key_invalid 标记
            if isinstance(e, LLMKeyMissingError):
                api_key_invalid = True
                logger.warning(f"[JSONContent] API key missing or invalid for {mode_id}: {e}")
            elif isinstance(e, HTTPStatusError):
                status_code = e.response.status_code if hasattr(e, "response") and e.response else None
                if status_code in (401, 403):
                    api_key_invalid = True
                    logger.warning(f"[JSONContent] API key invalid or expired for {mode_id}: HTTP {status_code}")
            elif isinstance(e, OpenAIError):
                # OpenAI/兼容 SDK 的错误可能包含状态码或错误码信息
                # 这里只把「鉴权相关」错误视为 API key 问题，避免把诸如 Model Not Exist 也误判为 key 失效。
                error_message = str(e).lower()
                error_code = getattr(e, "status_code", None) or getattr(e, "code", None)
                if (
                    error_code in (401, 403)
                    or "401" in error_message
                    or "403" in error_message
                    or "unauthorized" in error_message
                    or "auth" in error_message
                    or "api key" in error_message
                    or "apikey" in error_message
                ):
                    api_key_invalid = True
                    logger.warning(f"[JSONContent] API key invalid or expired for {mode_id}: {e}")
            fb = dict(fallback)
            # 标记为使用兜底内容，便于前端/统计判断
            fb["_is_fallback"] = True
            fb["_used_fallback"] = True
            # Mark LLM status for downstream billing/observability.
            fb["_llm_used"] = True
            fb["_llm_ok"] = False
            if api_key_invalid:
                fb["_api_key_invalid"] = True
            return fb

        if ctype == "llm":
            result = _parse_llm_output(text, content_cfg, fallback)
        elif ctype == "llm_json":
            result = _parse_llm_json_output(text, content_cfg, fallback)
        else:
            result = {"text": text}

        if not _validate_content_quality(result, content_cfg.get("output_schema")):
            logger.warning(f"[JSONContent] Quality check failed for {mode_id}, using fallback")
            if DISABLE_FALLBACK:
                result["_is_fallback"] = True
                result["_llm_used"] = True
                result["_llm_ok"] = llm_ok
                return _apply_post_process(result, content_cfg)
            fb = _apply_post_process(dict(fallback), content_cfg)
            fb["_is_fallback"] = True
            fb["_used_fallback"] = True
            fb["_llm_used"] = True
            fb["_llm_ok"] = llm_ok
            return fb

        content_hash = _compute_content_hash(result)
        if content_hash not in recent_hashes:
            break
        logger.info(f"[JSONContent] Dedup retry {attempt + 1} for {mode_id} (hash collision)")

    result = _apply_post_process(result, content_cfg)
    result = await _prefetch_images(result, mode_def)
    # Mark LLM status for downstream billing/observability.
    result["_llm_used"] = True
    result["_llm_ok"] = True
    return result


async def _generate_computed_content(mode_def: dict, content_cfg: dict, fallback: dict, **kwargs) -> dict:
    provider = content_cfg.get("provider", "")
    if provider == "countdown":
        from .content import generate_countdown_content
        config = content_cfg.get("config", {})
        cfg = dict(config if config else (kwargs.get("config") or {}))
        mode_settings = (kwargs.get("config") or {}).get("mode_settings", {})
        if isinstance(mode_settings, dict):
            events = mode_settings.get("countdownEvents")
            if isinstance(events, list):
                cfg["countdownEvents"] = events
        mode_overrides = (kwargs.get("config") or {}).get("mode_overrides", {})
        if isinstance(mode_overrides, dict):
            override = mode_overrides.get("COUNTDOWN")
            if isinstance(override, dict):
                override_events = override.get("countdownEvents")
                if not isinstance(override_events, list):
                    override_events = override.get("events")
                if isinstance(override_events, list):
                    cfg["countdownEvents"] = [
                        {
                            "name": str(ev.get("name", "")),
                            "date": str(ev.get("date", "")),
                            "type": "countup" if ev.get("type") == "countup" else "countdown",
                        }
                        for ev in override_events
                        if isinstance(ev, dict)
                    ]
        return await generate_countdown_content(config=cfg)
    if provider == "daily_meta":
        date_ctx = kwargs.get("date_ctx", {}) or {}
        lang = kwargs.get("language", "zh")
        result = dict(fallback)
        if lang == "en":
            _MONTH_EN = ["January", "February", "March", "April", "May", "June",
                         "July", "August", "September", "October", "November", "December"]
            _WEEKDAY_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            _now = datetime.now()
            month_idx = _now.month - 1
            weekday_idx = date_ctx.get("weekday", _now.weekday())
            result.update({
                "year": date_ctx.get("year"),
                "day": date_ctx.get("day"),
                "month_cn": _MONTH_EN[month_idx],
                "weekday_cn": _WEEKDAY_EN[weekday_idx],
                "day_of_year": date_ctx.get("day_of_year"),
                "days_in_year": date_ctx.get("days_in_year"),
            })
        else:
            lunar_date = fallback.get("lunar_date", "")
            lunar_date_display = fallback.get("lunar_date_display", "")
            try:
                from zhdate import ZhDate
                lunar_source = datetime.fromisoformat(_resolve_almanac_date(date_ctx))
                lunar_date = f"农历{ZhDate.from_datetime(lunar_source).chinese()}"
                lunar_date_display = _normalize_lunar_display(lunar_date)
            except (ImportError, ValueError, OverflowError):
                lunar_date = fallback.get("lunar_date", "")
                lunar_date_display = fallback.get("lunar_date_display", "")
            result.update({
                "year": date_ctx.get("year"),
                "day": date_ctx.get("day"),
                "month_cn": date_ctx.get("month_cn"),
                "weekday_cn": date_ctx.get("weekday_cn"),
                "day_of_year": date_ctx.get("day_of_year"),
                "days_in_year": date_ctx.get("days_in_year"),
                "lunar_date": lunar_date,
                "lunar_date_display": lunar_date_display,
            })
        return result
    if provider == "almanac_api":
        date_ctx = kwargs.get("date_ctx", {}) or {}
        lang = str(kwargs.get("language") or "zh").strip().lower()
        cache_date = _resolve_almanac_date(date_ctx)
        cached = await _get_cached_almanac_payload(cache_date)
        if cached:
            merged = dict(fallback)
            if lang == "en":
                translated = _get_almanac_translation(cached, "en")
                if translated:
                    merged.update(translated)
                    return merged
            else:
                merged.update(_base_almanac_payload(cached))
                return merged
        async with _ALMANAC_FETCH_LOCK:
            cached = await _get_cached_almanac_payload(cache_date)
            if cached:
                merged = dict(fallback)
                if lang == "en":
                    translated = _get_almanac_translation(cached, "en")
                    if not translated:
                        translated = await _translate_almanac_payload_to_en(cached, fallback)
                        if translated:
                            updated = dict(cached)
                            translations = updated.get("translations")
                            if not isinstance(translations, dict):
                                translations = {}
                            translations["en"] = translated
                            updated["translations"] = translations
                            await _save_cached_almanac_payload(cache_date, updated)
                    if translated:
                        merged.update(translated)
                        return merged
                else:
                    merged.update(_base_almanac_payload(cached))
                    return merged
            raw = await _fetch_tianapi_almanac_payload(cache_date)
            if raw:
                payload = _summarize_almanac_payload(raw, fallback)
                payload["health_tip"] = await _generate_almanac_health_tip(
                    raw,
                    str(fallback.get("health_tip") or "").strip(),
                )
                if lang == "en":
                    translated = await _translate_almanac_payload_to_en(payload, fallback)
                    if translated:
                        payload["translations"] = {"en": translated}
                await _save_cached_almanac_payload(cache_date, payload)
                merged = dict(fallback)
                if lang == "en":
                    translated = _get_almanac_translation(payload, "en")
                    if translated:
                        merged.update(translated)
                        return merged
                else:
                    merged.update(_base_almanac_payload(payload))
                    return merged
        return dict(fallback)
    if provider == "lifebar":
        import calendar
        now = datetime.now()
        date_ctx = kwargs.get("date_ctx", {}) or {}
        cfg = kwargs.get("config") or {}
        lang = kwargs.get("language", "zh")

        day_of_year = date_ctx.get("day_of_year") or now.timetuple().tm_yday
        days_in_year = date_ctx.get("days_in_year") or 365
        year_pct = round(day_of_year / days_in_year * 100, 1)

        days_in_month = calendar.monthrange(now.year, now.month)[1]
        month_pct = round(now.day / days_in_month * 100, 1)

        weekday_num = now.weekday() + 1
        week_pct = round(weekday_num / 7 * 100, 1)

        birth_year = int(cfg.get("birth_year", 0)) or 1995
        life_expect = int(cfg.get("life_expect", 0)) or 80
        age = now.year - birth_year
        life_pct = min(round(age / life_expect * 100, 1), 100.0)

        if lang == "en":
            _MONTH_EN_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            return {
                "year_pct": year_pct, "year_label": f"{now.year} elapsed",
                "month_pct": month_pct, "month_label": _MONTH_EN_SHORT[now.month - 1],
                "week_pct": week_pct, "week_label": "Week",
                "life_pct": life_pct, "life_label": "Life",
                "day_of_year": day_of_year, "days_in_year": days_in_year,
                "day": now.day, "days_in_month": days_in_month,
                "weekday_num": weekday_num, "week_total": 7,
                "age": age, "life_expect": life_expect,
            }
        return {
            "year_pct": year_pct, "year_label": f"{now.year} 年已过",
            "month_pct": month_pct, "month_label": f"{now.month}月",
            "week_pct": week_pct, "week_label": "本周",
            "life_pct": life_pct, "life_label": "人生",
            "day_of_year": day_of_year, "days_in_year": days_in_year,
            "day": now.day, "days_in_month": days_in_month,
            "weekday_num": weekday_num, "week_total": 7,
            "age": age, "life_expect": life_expect,
        }

    if provider == "memo":
        config = kwargs.get("config") or {}
        lang = kwargs.get("language", "zh")
        mode_settings = config.get("mode_settings", {}) if isinstance(config.get("mode_settings", {}), dict) else {}
        items: list[dict[str, str]] = []
        for i in range(1, 4):
            tk = f"memo_title_{i}"
            tv = mode_settings.get(tk, "") if isinstance(mode_settings.get(tk, ""), str) else ""
            if not tv:
                tv = config.get(tk, "")
            tv = tv if isinstance(tv, str) else ""
            ck = f"memo_text_{i}"
            cv = mode_settings.get(ck, "") if isinstance(mode_settings.get(ck, ""), str) else ""
            if not cv:
                cv = config.get(ck, "")
            cv = cv if isinstance(cv, str) else ""
            if tv or cv:
                items.append({"title": tv, "text": cv})
        if not items:
            fb_items = fallback.get("memo_items")
            if isinstance(fb_items, list):
                items = fb_items
            else:
                default_title = "TODO" if lang == "en" else "今日待办"
                items = [{"title": default_title, "text": "1. \n2. \n3. "}]
        return {"memo_items": items}

    if provider == "vocab_review":
        from .vocab_store import get_vocab_content

        mac = str(kwargs.get("mac") or "").strip()
        if not mac:
            return dict(fallback)
        result = dict(fallback)
        result.update(await get_vocab_content(mac, kwargs.get("config") or {}))
        return result

    if provider == "habit":
        config = kwargs.get("config") or {}
        lang = kwargs.get("language", "zh")
        configured_items = None
        mo = config.get("mode_overrides", {})
        if isinstance(mo, dict):
            habit_ov = mo.get("HABIT", {})
            if isinstance(habit_ov, dict):
                configured_items = habit_ov.get("habitItems")

        habits = []
        if isinstance(configured_items, list) and configured_items:
            for item in configured_items:
                if isinstance(item, dict):
                    n = item.get("name", "")
                    done = bool(item.get("done", False))
                elif isinstance(item, str):
                    n = item
                    done = False
                else:
                    continue
                if n:
                    habits.append({"name": n, "done": done, "status": "●" if done else "○"})

        completed = sum(1 for h in habits if h.get("done"))
        total = len(habits) if habits else 0

        _HABIT_MOTTOS_ZH = [
            "业精于勤荒于嬉",
            "千里之行始于足下",
            "贵在坚持",
            "每天进步一点点",
            "自律即自由",
            "好习惯成就好人生",
            "日拱一卒，功不唐捐",
            "积跬步以至千里",
            "今日事今日毕",
            "坚持就是胜利",
        ]
        _HABIT_MOTTOS_EN = [
            "Consistency is key",
            "Small steps, big results",
            "Discipline equals freedom",
            "Progress, not perfection",
            "One day at a time",
            "Good habits shape great lives",
            "Keep going, you're doing great",
            "Every effort counts",
            "Stay the course",
            "Success is built daily",
        ]
        import random
        motto = random.choice(_HABIT_MOTTOS_EN if lang == "en" else _HABIT_MOTTOS_ZH)

        if habits:
            habit_lines = [f"{h['status']}  {h['name']}" for h in habits]
            habit_list = "\n".join(habit_lines)
            if lang == "en":
                habit_footer = f"Completed {completed}/{total} today · {motto}"
            else:
                habit_footer = f"今日已完成 {completed}/{total} 项 · {motto}"
        else:
            habit_list = fallback.get("summary", "")
            habit_footer = motto
        # 保留 summary 兼容旧布局
        summary_lines = [f"{h['name']} {h['status']}" for h in habits] if habits else []
        if habits:
            if lang == "en":
                summary_lines.append(f"\nCompleted {completed}/{total} today")
            else:
                summary_lines.append(f"\n今日已完成 {completed}/{total} 项")
        summary = "\n".join(summary_lines) if summary_lines else fallback.get("summary", "")
        return {
            "habits": habits,
            "summary": summary,
            "habit_list": habit_list,
            "habit_footer": habit_footer,
            "week_progress": completed,
            "week_total": total,
        }

    if provider == "calendar_grid":
        import calendar as cal_mod
        from zhdate import ZhDate
        from .config import SOLAR_FESTIVALS, LUNAR_FESTIVALS, SOLAR_TERMS

        lang = kwargs.get("language", "zh")
        is_en = lang == "en"

        EN_HOLIDAYS: dict[tuple[int, int], str] = {
            (1, 1): "New Year",
            (2, 14): "Valentine",
            (3, 8): "Women's",
            (3, 17): "St Patrick",
            (4, 1): "April Fool",
            (4, 22): "Earth Day",
            (5, 1): "May Day",
            (6, 1): "Children",
            (7, 4): "July 4th",
            (10, 31): "Halloween",
            (11, 11): "Veterans",
            (12, 24): "Xmas Eve",
            (12, 25): "Christmas",
            (12, 31): "NYE",
        }

        def _en_floating_holidays(y: int, m: int) -> dict[int, str]:
            result: dict[int, str] = {}
            if m == 1:
                _jan1_wd = datetime(y, 1, 1).weekday()
                mlk = 15 + (0 - _jan1_wd) % 7 + 7
                result[mlk] = "MLK Day"
            if m == 2:
                _feb1_wd = datetime(y, 2, 1).weekday()
                pres = 15 + (0 - _feb1_wd) % 7 + 7
                result[pres] = "President"
            if m == 5:
                last_mon = 31
                while datetime(y, 5, last_mon).weekday() != 0:
                    last_mon -= 1
                result[last_mon] = "Memorial"
            if m == 9:
                _sep1_wd = datetime(y, 9, 1).weekday()
                labor = 1 + (0 - _sep1_wd) % 7
                result[labor] = "Labor Day"
            if m == 10:
                _oct1_wd = datetime(y, 10, 1).weekday()
                columbus = 8 + (0 - _oct1_wd) % 7
                result[columbus] = "Columbus"
            if m == 11:
                _nov1_wd = datetime(y, 11, 1).weekday()
                thx = 22 + (3 - _nov1_wd) % 7
                result[thx] = "Thxgiving"
            if m == 3 or m == 4:
                import math
                a = y % 19; b, c = divmod(y, 100); d, e = divmod(b, 4)
                f = (b + 8) // 25; g = (b - f + 1) // 3
                h = (19 * a + b - d - g + 15) % 30; i, k = divmod(c, 4)
                l = (32 + 2 * e + 2 * i - h - k) % 7
                em = (a + 11 * h + 22 * l) // 451
                easter_m = (h + l - 7 * em + 114) // 31
                easter_d = ((h + l - 7 * em + 114) % 31) + 1
                if easter_m == m:
                    result[easter_d] = "Easter"
            return result

        LUNAR_DAY_NAMES = [
            "", "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
            "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
            "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十",
        ]
        _MONTH_EN = ["", "January", "February", "March", "April", "May", "June",
                     "July", "August", "September", "October", "November", "December"]

        now = datetime.now()
        year, month, day = now.year, now.month, now.day
        first_weekday, days_in_month = cal_mod.monthrange(year, month)
        rows: list[list[str]] = []
        week: list[str] = [""] * first_weekday
        for d in range(1, days_in_month + 1):
            week.append(str(d))
            if len(week) == 7:
                rows.append(week)
                week = []
        if week:
            week.extend([""] * (7 - len(week)))
            rows.append(week)

        config = kwargs.get("config") or {}
        mode_settings = config.get("mode_settings", {})
        if not isinstance(mode_settings, dict):
            mode_settings = {}
        reminders = mode_settings.get("reminders", {})
        if not isinstance(reminders, dict):
            reminders = {}
        mo = config.get("mode_overrides", {})
        if isinstance(mo, dict):
            cal_ov = mo.get("CALENDAR", {})
            if isinstance(cal_ov, dict) and isinstance(cal_ov.get("reminders"), dict):
                reminders = {**reminders, **cal_ov["reminders"]}

        day_labels: dict[str, str] = {}
        day_label_types: dict[str, str] = {}
        today_lunar = ""
        today_festival = ""
        for d in range(1, days_in_month + 1):
            ds = str(d)
            reminder_key = f"{month}-{d}"
            if reminder_key in reminders:
                text = str(reminders[reminder_key])
                max_len = 7
                day_labels[ds] = (text[:max_len] + "…") if len(text) > max_len else text
                day_label_types[ds] = "reminder"
                continue
            if is_en:
                floating = _en_floating_holidays(year, month)
                hol = EN_HOLIDAYS.get((month, d), "") or floating.get(d, "")
                day_labels[ds] = hol
                day_label_types[ds] = "festival" if hol else ""
                if d == day:
                    today_festival = hol
                continue
            solar_fest = SOLAR_FESTIVALS.get((month, d), "")
            solar_term = SOLAR_TERMS.get((year, month, d), "")
            try:
                zh = ZhDate.from_datetime(datetime(year, month, d))
                lunar_fest = LUNAR_FESTIVALS.get((zh.lunar_month, zh.lunar_day), "")
                lunar_name = LUNAR_DAY_NAMES[zh.lunar_day] if zh.lunar_day < len(LUNAR_DAY_NAMES) else ""
            except (ValueError, OverflowError):
                lunar_fest = ""
                lunar_name = ""

            if solar_fest or lunar_fest:
                day_labels[ds] = solar_fest or lunar_fest
                day_label_types[ds] = "festival"
            elif solar_term:
                day_labels[ds] = solar_term
                day_label_types[ds] = "solar_term"
            else:
                day_labels[ds] = lunar_name
                day_label_types[ds] = "lunar"

            if d == day:
                try:
                    zh_today = ZhDate.from_datetime(now)
                    today_lunar = f"农历{zh_today.chinese()}"
                except (ValueError, OverflowError):
                    today_lunar = ""
                today_festival = solar_fest or lunar_fest or solar_term

        today_key = f"{month}-{day}"
        if is_en:
            reminder_hint = f"Today's reminder: {reminders[today_key]}" if today_key in reminders else ""
            cal_title = f"{_MONTH_EN[month]} {year}"
            weekday_headers = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
        else:
            reminder_hint = f"今天的提醒: {reminders[today_key]}" if today_key in reminders else ""
            cal_title = f"{year}年{month}月"
            weekday_headers = ["一", "二", "三", "四", "五", "六", "日"]

        return {
            "calendar_title": cal_title,
            "weekday_headers": weekday_headers,
            "calendar_rows": rows,
            "today_day": str(day),
            "day_labels": day_labels,
            "day_label_types": day_label_types,
            "lunar_date": today_lunar,
            "festival": today_festival,
            "reminder_hint": reminder_hint,
        }

    if provider == "timetable":
        lang = kwargs.get("language", "zh")
        is_en = lang == "en"
        now = datetime.now()
        config = kwargs.get("config") or {}
        mode_settings = config.get("mode_settings", {})
        if not isinstance(mode_settings, dict):
            mode_settings = {}
        mo = config.get("mode_overrides", {})
        if isinstance(mo, dict):
            tt_ov = mo.get("TIMETABLE", {})
            if isinstance(tt_ov, dict):
                if "style" in tt_ov:
                    mode_settings = {**mode_settings, **tt_ov}

        style = str(mode_settings.get("style", "daily"))
        periods = mode_settings.get("periods")
        courses = mode_settings.get("courses")

        if not isinstance(periods, list) or not isinstance(courses, dict):
            style = "weekly"
            periods = ["08:00-09:30", "10:00-11:30", "14:00-15:30", "16:00-17:30"]
            if is_en:
                courses = {
                    "0-0": "Calculus/A201", "0-2": "Linear Algebra/A201",
                    "1-1": "English/B305", "1-3": "PE/Gym",
                    "2-0": "Data Struct/C102", "2-2": "Networks/C102",
                    "3-1": "Probability/A201", "3-3": "Politics/D405",
                    "4-0": "OS/C102",
                }
            else:
                courses = {
                    "0-0": "高等数学/A201", "0-2": "线性代数/A201",
                    "1-1": "大学英语/B305", "1-3": "体育/操场",
                    "2-0": "数据结构/C102", "2-2": "计算机网络/C102",
                    "3-1": "概率论/A201", "3-3": "毛概/D405",
                    "4-0": "操作系统/C102",
                }

        if is_en:
            weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            weekdays_short = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        else:
            weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            weekdays_short = ["一", "二", "三", "四", "五"]

        custom_weekdays = mode_settings.get("weekdays")
        if isinstance(custom_weekdays, list):
            cleaned_weekdays = [str(item).strip() for item in custom_weekdays if str(item).strip()]
            if cleaned_weekdays:
                weekdays_short = cleaned_weekdays[:7]

        wd = now.weekday()
        current_day = wd if wd < len(weekdays_short) else -1

        current_period = -1
        for pi, p_label in enumerate(periods):
            try:
                start_part = p_label.split("-")[0].strip()
                parts = start_part.replace("：", ":").split(":")
                h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
                if now.hour > h or (now.hour == h and now.minute >= m):
                    current_period = pi
            except (ValueError, IndexError):
                pass

        if style == "weekly":
            grid: list[list[str]] = []
            for pi in range(len(periods)):
                row = []
                for di in range(len(weekdays_short)):
                    row.append(str(courses.get(f"{di}-{pi}", "")))
                grid.append(row)
            if is_en:
                title = f"{weekday_names[wd]} · This Week" if wd < 7 else "This Week"
            else:
                title = f"{weekday_names[wd]} · 本周课程" if wd < 7 else "本周课程"
            return {
                "style": "weekly",
                "periods": periods,
                "grid": grid,
                "current_day": current_day,
                "current_period": current_period,
                "weekdays": weekdays_short,
                "timetable_title": title,
            }

        slots = []
        if current_day >= 0:
            for pi, p_label in enumerate(periods):
                val = str(courses.get(f"{current_day}-{pi}", ""))
                if not val:
                    continue
                if "/" in val:
                    name, location = val.split("/", 1)
                else:
                    name, location = val, ""
                slots.append({
                    "time": p_label,
                    "name": name,
                    "location": location,
                    "current": pi == current_period,
                })
        if is_en:
            title = f"{weekday_names[wd]} · Today" if wd < 7 else "Today"
        else:
            title = f"{weekday_names[wd]} · 今日课程" if wd < 7 else "今日课程"
        return {
            "style": "daily",
            "timetable_title": title,
            "slots": slots,
            "slot_count": len(slots),
            "current_day": current_day,
            "current_period": current_period,
        }

    return dict(fallback)


async def _generate_external_data_content(mode_def: dict, content_cfg: dict, fallback: dict, **kwargs) -> dict:
    from .content import (
        fetch_hn_top_stories,
        fetch_ph_top_product,
        fetch_devto_top,
    )

    provider = content_cfg.get("provider", "")
    llm_provider = kwargs.get("llm_provider") or DEFAULT_LLM_PROVIDER
    llm_model = kwargs.get("llm_model") or DEFAULT_LLM_MODEL
    api_key = kwargs.get("api_key")
    llm_base_url = kwargs.get("llm_base_url")
    language = kwargs.get("language", "zh") or "zh"

    if provider == "briefing":
        hn_limit = int(content_cfg.get("hn_limit", 2))
        devto_limit = int(content_cfg.get("devto_limit", 1))
        devto_fallback_title = "Dev.to unavailable" if language == "en" else "Dev.to 暂无数据"

        import asyncio as _asyncio
        hn_items, ph_item, devto_items = await _asyncio.gather(
            fetch_hn_top_stories(limit=hn_limit),
            fetch_ph_top_product(),
            fetch_devto_top(limit=devto_limit),
        )
        if not hn_items and not ph_item and not devto_items:
            fb = dict(fallback)
            fb["_is_fallback"] = True
            fb["_used_fallback"] = True
            fb["_llm_used"] = False
            fb["_llm_ok"] = False
            return fb

        result = dict(fallback)
        ph_name = ""
        ph_tagline = ""
        if isinstance(ph_item, dict):
            ph_name = str(ph_item.get("name", ""))
            ph_tagline = str(ph_item.get("tagline", ""))
        devto_title = ""
        if isinstance(devto_items, list) and devto_items:
            devto_title = str(devto_items[0].get("title", ""))
        result.update({
            "hn_items": hn_items or result.get("hn_items", []),
            "ph_item": ph_item or result.get("ph_item", {}),
            "devto_items": devto_items or [{"title": devto_fallback_title}],
            "ph_name": ph_name,
            "ph_tagline": ph_tagline,
            "devto_title": devto_title or devto_fallback_title,
        })
        result = await _translate_briefing_result(result, language)
        result["_llm_used"] = False
        result["_llm_ok"] = False
        return result

    if provider == "weather_forecast":
        from .context import extract_location_settings, get_weather_forecast
        try:
            config = kwargs.get("config") or {}
            mode_settings = config.get("mode_settings", {}) if isinstance(config.get("mode_settings", {}), dict) else {}
            days = mode_settings.get("forecast_days", 4)
            if not isinstance(days, int):
                days = 4
            days = max(1, min(7, days))
            data = await get_weather_forecast(
                days=days,
                language=kwargs.get("language", "zh") or "zh",
                **extract_location_settings(config),
            )
            if not data:
                return dict(fallback)
            if not data.get("today_temp") or data["today_temp"] == "--":
                return dict(fallback)
            merged = dict(fallback)
            merged.update(data)
            return merged
        except (httpx.HTTPError, TypeError, ValueError, JSONDecodeError) as e:
            logger.warning(f"[JSONContent] Failed to get weather forecast: {e}", exc_info=True)
            return dict(fallback)

    if provider == "ai_usage":
        return await _generate_ai_usage_content(fallback, **kwargs)

    if provider == "hotlist":
        return await _generate_hotlist_content(content_cfg, fallback, **kwargs)

    return dict(fallback)


# ── 全网热点榜单（多源回退 + TTL 缓存）───────────────────────
_HOTLIST_SOURCES = [
    ("weibo_ajax", "https://weibo.com/ajax/side/hotSearch",
     {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}),
    ("vvhan", "https://api.vvhan.com/api/hotlist?type=weibo", {}),
    ("imsyy", "https://api-hot.imsyy.top/weibo?cache=true", {}),
    ("tenapi", "https://tenapi.cn/v2/weibohot", {}),
]
_HOTLIST_CACHE: dict[str, tuple[float, list[dict]]] = {}
_HOTLIST_CACHE_TTL = 600  # 10 分钟


def _parse_hotlist(source: str, data) -> list[dict]:
    """把各来源响应规整为 [{rank, title, hot}]。"""
    items: list[dict] = []
    try:
        if source == "weibo_ajax":
            realtime = (data.get("data") or {}).get("realtime") or []
            for it in realtime:
                if isinstance(it, dict) and it.get("word"):
                    items.append({"rank": len(items) + 1, "title": it["word"],
                                  "hot": it.get("num", "")})
        elif source == "vvhan":
            for it in (data.get("data") or []):
                if isinstance(it, dict) and it.get("title"):
                    items.append({"rank": len(items) + 1, "title": it["title"],
                                  "hot": it.get("hot", "")})
        elif source == "imsyy":
            for it in (data.get("data") or []):
                if isinstance(it, dict) and it.get("title"):
                    items.append({"rank": len(items) + 1, "title": it["title"],
                                  "hot": it.get("hot", "")})
        elif source == "tenapi":
            for it in (data.get("data") or []):
                if isinstance(it, dict) and it.get("word"):
                    items.append({"rank": len(items) + 1, "title": it["word"],
                                  "hot": it.get("num", "")})
    except Exception:  # noqa: BLE001
        items = []
    return items


async def _fetch_hotlist(max_items: int = 10) -> list[dict]:
    import time as _t

    now = _t.time()
    cached = _HOTLIST_CACHE.get("weibo")
    if cached and now - cached[0] < _HOTLIST_CACHE_TTL:
        return cached[1][:max_items]
    last_err = None
    for source, url, headers in _HOTLIST_SOURCES:
        try:
            async with httpx.AsyncClient(timeout=8, headers=headers) as client:
                r = await client.get(url)
                r.raise_for_status()
                data = r.json()
            items = _parse_hotlist(source, data)
            if items:
                _HOTLIST_CACHE["weibo"] = (now, items)
                return items[:max_items]
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    logger.warning("[HOTLIST] all sources failed: %s", last_err)
    return []


async def _generate_hotlist_content(content_cfg: dict, fallback: dict, **kwargs) -> dict:
    """provider=hotlist：全网热点榜单（默认微博热搜，多源回退）。"""
    result = dict(fallback)
    ms = kwargs.get("config", {}).get("mode_settings") if isinstance(kwargs.get("config"), dict) else None
    max_items = 10
    if isinstance(ms, dict):
        try:
            max_items = int(ms.get("max_items", 10))
        except (TypeError, ValueError):
            max_items = 10
    max_items = max(1, min(20, max_items))
    try:
        items = await _fetch_hotlist(max_items)
    except Exception as e:  # noqa: BLE001
        logger.warning("[HOTLIST] fetch failed: %s", e)
        items = []
    if items:
        result["items"] = items
        result["source_label"] = "微博热搜"
    else:
        result["items"] = list(result.get("items") or [])[:max_items]
    return result


async def _generate_ai_usage_content(fallback: dict, **kwargs) -> dict:
    """provider=ai_usage：DeepSeek 余额（官方 /user/balance）+ Codex 订阅额度（本机同步）。

    输出扁平字段（供 json_renderer / structured_payload 使用）：
      deepseek_balance / deepseek_currency / deepseek_granted / deepseek_topped_up /
      deepseek_available / deepseek_extra / codex_windows_text / codex_meta
    """
    import os

    result = dict(fallback)
    mac = str(kwargs.get("mac") or "")
    api_key = str(kwargs.get("api_key") or "").strip() or (os.getenv("DEEPSEEK_API_KEY") or "").strip()

    # ── DeepSeek 余额（官方接口）──────────────────────────────
    _last_ds = _AI_USAGE_LAST_DS  # module-level: {mac: (balance, currency)}
    if api_key:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    "https://api.deepseek.com/user/balance",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                r.raise_for_status()
                data = r.json()
            bi = {}
            if isinstance(data, dict):
                infos = data.get("balance_infos") or []
                # 账户可能同时有 USD 与 CNY：优先 CNY（充值），否则第一条
                entries = [it for it in infos if isinstance(it, dict)]
                for it in entries:
                    if str(it.get("currency") or "").upper() in ("CNY", "RMB"):
                        bi = it
                        break
                else:
                    bi = entries[0] if entries else {}
            result.update({
                "deepseek_available": bool(data.get("is_available")) if isinstance(data, dict) else False,
                "deepseek_balance": bi.get("total_balance", "--"),
                "deepseek_currency": bi.get("currency", "CNY"),
                "deepseek_granted": bi.get("granted_balance", "--"),
                "deepseek_topped_up": bi.get("topped_up_balance", "--"),
            })
            # 成功时记录 last-good（供接口抖动兜底）
            try:
                bal_f = float(str(result["deepseek_balance"]).replace(",", ""))
                if str(result["deepseek_balance"]) not in ("--", "", "None") and bal_f >= 0:
                    _last_ds[mac] = (result["deepseek_balance"], result["deepseek_currency"])
            except (TypeError, ValueError):
                pass
            # 持久缓存（第一阶段可靠层）：按 currency 独立落库 CNY/USD，合法 0 接受
            try:
                from .reliable_sources import cache_deepseek_result
                cache_deepseek_result(data)
            except Exception:  # noqa: BLE001
                logger.debug("[AI_USAGE] deepseek persistent cache write skipped")
            result["deepseek_extra"] = (
                f"可用 {'是' if result['deepseek_available'] else '否'} · "
                f"充值 {result['deepseek_topped_up']} · 赠送 {result['deepseek_granted']}"
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as e:
            logger.warning("[AI_USAGE] DeepSeek balance failed: %s", e, exc_info=True)
            if mac in _last_ds:
                bal, cur = _last_ds[mac]
                result.update({
                    "deepseek_available": True,
                    "deepseek_balance": bal,
                    "deepseek_currency": cur,
                    "deepseek_granted": "--",
                    "deepseek_topped_up": bal,
                })
                result["deepseek_extra"] = f"可用 是 · 充值 {bal} · 赠送 --（接口抖动，用上次值）"
            else:
                # 持久缓存兜底（重启后仍有效；失败不清零）
                try:
                    from .data_cache import get_group as _dc_get
                    g = _dc_get("ai.ds.cny") or {}
                    v = g.get("value")
                    if v is not None and g.get("status") in ("fresh", "stale"):
                        result.update({
                            "deepseek_available": True,
                            "deepseek_balance": str(v),
                            "deepseek_currency": "CNY",
                            "deepseek_granted": "--",
                            "deepseek_topped_up": str(v),
                        })
                        result["deepseek_extra"] = f"可用 是 · 充值 {v} · 赠送 --（持久缓存兜底）"
                    else:
                        result["deepseek_extra"] = "余额查询不可用"
                except Exception:  # noqa: BLE001
                    result["deepseek_extra"] = "余额查询不可用"
    else:
        result["deepseek_extra"] = "未配置 DeepSeek key"

    # ── Codex 订阅额度（本机同步客户端推送的记录）──────────────
    codex = None
    try:
        from .codex_usage_store import get_codex_usage, get_codex_usage_any
        from .freshness_config import ai_source_policy

        _fresh_policy = ai_source_policy()
        _codex_stale_s = _fresh_policy["codex"]["stale_after_s"]

        if mac:
            codex = await get_codex_usage(mac, stale_seconds=_codex_stale_s)
        else:
            # 云端发布端拉内容不带设备上下文 → 读全局最新（Mac 权威）
            codex = await get_codex_usage_any(stale_seconds=_codex_stale_s)
    except Exception as e:  # noqa: BLE001
        logger.warning("[AI_USAGE] codex_usage read failed: %s", e)
    if codex and codex.get("windows"):
        # 设备主字段取 7D 窗口（第一阶段可靠层）：10080min 置顶，
        # 避免多桶/顺序变化时把 5H 等当七日额度；无 7D 时保留原顺序并标 stale
        from .reliable_sources import SEVEN_DAY_MINUTES
        entries = []
        for w in codex.get("windows", []):
            used = w.get("used_percent")
            if used is None:
                continue
            entries.append({
                "label": w.get("label") or "",
                "duration_minutes": int(w.get("duration_minutes") or 0),
                "used_percent": used,
                "resets_at": w.get("resets_at"),
            })
        entries.sort(key=lambda x: 0 if x["duration_minutes"] == SEVEN_DAY_MINUTES else 1)
        if not any(x["duration_minutes"] == SEVEN_DAY_MINUTES for x in entries):
            codex = dict(codex)
            codex["no_7d_window"] = True
        lines = []
        structured_windows = []
        for x in entries:
            lines.append(f"{x['label']} 剩余 {100 - x['used_percent']:.0f}% · 已用 {x['used_percent']:.0f}%")
            structured_windows.append({
                "label": x["label"],
                "duration_minutes": x["duration_minutes"],
                "used_percent": x["used_percent"],
                "remaining_pct": round(100.0 - x["used_percent"], 1),
                "resets_at": x.get("resets_at"),
            })
        result["codex_windows_text"] = "\n".join(lines) or "暂无额度数据"
        result["codex_windows"] = structured_windows  # 供 structured_payload 进度条/剩余大字
        meta = f"来源 {codex.get('source') or '本机'}"
        _plan = codex.get("plan")
        if _plan:
            meta += f" · {str(_plan).capitalize()} 计划"
        if codex.get("stale"):
            meta += " · 数据过期"
        if codex.get("no_7d_window"):
            meta += " · 无 7D 窗口（unknown）"
        result["codex_meta"] = meta
        # 持久缓存（第一阶段可靠层）：7D 窗口与重置/会员落库供版本与统一文档使用
        try:
            from .data_cache import record_failure as _dc_fail, record_success as _dc_ok
            seven = next((x for x in structured_windows
                          if x.get("duration_minutes") == SEVEN_DAY_MINUTES), None)
            if seven is not None and not codex.get("stale"):
                _dc_ok("ai.codex", {
                    "used_percent": seven["used_percent"],
                    "resets_at": seven.get("resets_at"),
                    "duration_minutes": 10080,
                    "plan": codex.get("plan"),
                    "reset_credits_available": codex.get("reset_credits_available"),
                    "reset_expiry_list": codex.get("reset_expiry_list"),
                    "reset_expiry_source": codex.get("reset_expiry_source"),
                    "reset_expiry_note": codex.get("reset_expiry_note"),
                    "credit_balance": codex.get("credit_balance"),
                    "credit_has_credits": codex.get("credit_has_credits"),
                    "credit_unlimited": codex.get("credit_unlimited"),
                    "account_key": codex.get("account_key"),
                    "source": codex.get("source"),
                }, meta={"currency": None, "unit": "percent"},
                    now=float(codex.get("ts") or time.time()))
            elif seven is not None:
                _dc_fail("ai.codex", note="cached Codex sample exceeded expected collection cycles")
            else:
                _dc_fail("ai.codex", note="no 7D window in codex payload")
        except Exception:  # noqa: BLE001
            logger.debug("[AI_USAGE] codex persistent cache write skipped")
    else:
        result["codex_windows_text"] = "暂无额度数据"
        result["codex_meta"] = "等待本机 Codex 同步"

    # 第二阶段：附加新闻/金价/版本/布局信息（读可靠缓存，不触发采集）
    try:
        from . import data_cache as _dc
        from .news_feed import items_cached as _news_cached
        from .gold_feed import cached as _gold_cached
        # 科技/AI 简报（news.items）优先；无 current brief 时沿用旧三分类新闻结构
        _brief = None
        try:
            from .news_brief import fetch_news_for_publish as _brief_news
            _brief = _brief_news()
        except Exception:  # noqa: BLE001
            _brief = None
        result["feed_news"] = copy.deepcopy(_brief if _brief is not None else _news_cached())
        if isinstance(result["feed_news"], dict):
            try:
                from .news_schedule import current_due_status as _news_due_status
                _due = _news_due_status()
                _state = str(_due.get("state") or "")
                if _state in {"due", "waiting", "generating"}:
                    result["feed_news"]["update_state"] = "due"
                elif _state in {"failed", "expired"} and not _due.get("current"):
                    result["feed_news"]["update_state"] = "not-updated"
                else:
                    result["feed_news"]["update_state"] = "current"
                result["feed_news"]["expected_schedule_id"] = _due.get("schedule_id")
            except Exception:  # noqa: BLE001
                result["feed_news"]["update_state"] = "unknown"
        result["feed_gold"] = _gold_cached()
        _news_ver = None
        if _brief is not None:
            try:
                from .news_brief import _load_state as _brief_state
                _news_ver = (_brief_state().get("current") or {}).get("version")
            except Exception:  # noqa: BLE001
                _news_ver = None
        result["feed_versions"] = {
            "ai_key": _dc.version("ai_key"),
            "news": _news_ver or _dc.version("news"),
            "gold": _dc.version("gold"),
        }
        # AI 页权威数据段（第二阶段双面板；CNY/USD 独立、重置次数实际值、到期日由账户资料提供）
        _codex_group = _dc.get_group("ai.codex") or {}
        _c = _codex_group.get("value")
        _mem = {}
        try:
            from .reliable_sources import member_config, ds_values_cached
            _mem = member_config()
            _dsv = ds_values_cached()
        except Exception:  # noqa: BLE001
            _dsv = {"CNY": None, "USD": None}
        # 各来源最后成功时间（分组新鲜度 §8.2；设备/主机据其判定 stale；0=无/不适用）
        _last = lambda g: int((_dc.get_group(g) or {}).get("last_success") or 0)
        _fr = {"codex": _last("ai.codex"), "ds_cny": _last("ai.ds.cny"),
               "ds_usd": _last("ai.ds.usd"), "member": 0}
        try:
            from .freshness_config import ai_source_policy
            _fresh_policy = ai_source_policy()
        except Exception:  # noqa: BLE001
            _fresh_policy = {}
        result["feed_ai"] = {
            "codex_7d_used": (_c or {}).get("used_percent"),
            "codex_resets_at": (_c or {}).get("resets_at"),
            "codex_duration_minutes": (_c or {}).get("duration_minutes"),
            "reset_credits_available": (_c or {}).get("reset_credits_available"),
            # A stale last-known zero remains useful evidence, but it must not
            # trigger the balance fallback after a failed/unknown read.
            "reset_credits_confirmed": bool(
                _codex_group.get("status") == "fresh"
                and isinstance((_c or {}).get("reset_credits_available"), (int, float))
                and not isinstance((_c or {}).get("reset_credits_available"), bool)
            ),
            "reset_credits_status": str(_codex_group.get("status") or "unknown"),
            "codex_credit_balance": (_c or {}).get("credit_balance"),
            "codex_credit_has_credits": (_c or {}).get("credit_has_credits"),
            "codex_credit_unlimited": (_c or {}).get("credit_unlimited"),
            "codex_account_key": (_c or {}).get("account_key"),
            # 手动重置机会到期列表（Unix 秒，升序、未过期；缺失=None，空列表=[] 明确 0 次）
            # API 实时来源优先；缺失/失败时回退“手动配置”（来源 manual，仅用户确认后启用）；
            # 两者皆无 → None → 设备显示“到期时间暂不可用”（不冒充，不置空数组）。
            "reset_expiry_list": None,
            "reset_expiry_source": None,
            "reset_expiry_note": (_c or {}).get("reset_expiry_note"),
            "plan": (_mem or {}).get("plan"),
            "valid_until_date": (_mem or {}).get("valid_until_date"),
            "deepseek": {"CNY": _dsv.get("CNY"), "USD": _dsv.get("USD")},
            "member": _mem,
            "fresh": _fr,
            "freshness_policy": _fresh_policy,
        }
        try:
            from .deepseek_activity import effective_balances as _activity_balances
            result["feed_ai"]["deepseek_activity"] = _activity_balances(_dsv)
        except Exception:  # noqa: BLE001
            # Safe compatibility fallback: if attribution state is unavailable,
            # changes remain visible as activity rather than being hidden.
            result["feed_ai"]["deepseek_activity"] = dict(_dsv)
        try:
            # Read the same effective configuration used by structured_payload:
            # built-in defaults -> legacy compatibility -> public config.
            # This avoids a split-brain state where a value entered in the new
            # user config reaches the switches but not the displayed balances.
            from .operator_config import load_effective as _load_operator
            _display = dict((_load_operator().config.get("panel_display") or {}))
        except Exception:  # noqa: BLE001
            _display = {}
        result["feed_ai"]["display_preferences"] = {
            "show_codex_credits": bool(_display.get("show_codex_credits", False)),
            "show_openai_api_info": bool(_display.get("show_openai_api_info", False)),
            "show_reset_opportunities": bool(_display.get("show_reset_opportunities", True)),
        }
        # Automatic values only. Legacy manual balances remain readable in the
        # configuration document for migration audit but are never displayed or
        # used as a fallback.
        try:
            from .openai_costs import snapshot as _openai_cost_snapshot
            _cost = _openai_cost_snapshot()
        except Exception:  # noqa: BLE001
            _cost = {"complete": False, "amount": None, "state": "unavailable"}
        result["feed_ai"].update({
            "openai_api_month_spend_usd": (_cost.get("amount")
                                             if _cost.get("complete") is True else None),
            "openai_api_month_spend_complete": _cost.get("complete") is True,
            "openai_api_month_spend_state": _cost.get("state"),
            "openai_api_month": _cost.get("month"),
            "openai_api_month_basis": "UTC",
        })
        try:
            from .deepseek_token_tracker import snapshot as _token_snapshot
            _dst = _token_snapshot()
        except Exception:  # noqa: BLE001
            _dst = {"tokens": 0, "complete": False, "source": "unavailable"}
        result["feed_ai"].update({
            "deepseek_today_tokens": _dst.get("tokens"),
            "deepseek_today_tokens_complete": _dst.get("complete", False),
            "deepseek_today_tokens_source": _dst.get("source"),
        })
        # 到期列表：API 缓存优先；其次 manual 配置（明确来源）；未接通保持 None
        _api_list = (_c or {}).get("reset_expiry_list")
        if isinstance(_api_list, list) and (_c or {}).get("reset_expiry_source") == "api":
            result["feed_ai"]["reset_expiry_list"] = list(_api_list)
            result["feed_ai"]["reset_expiry_source"] = "api"
        else:
            try:
                from .manual_reset_config import load_manual_reset
                m = load_manual_reset()
                if m and isinstance(m.get("expiry_list"), list) and m["expiry_list"]:
                    now_i = int(__import__("time").time())
                    ok = [int(x) for x in m["expiry_list"] if isinstance(x, (int, float)) and int(x) > now_i]
                    if ok:
                        result["feed_ai"]["reset_expiry_list"] = sorted(set(ok))
                        result["feed_ai"]["reset_expiry_source"] = "manual"
            except Exception:  # noqa: BLE001
                logger.debug("[AI_USAGE] manual reset expiry load skipped")
        # Firmware-side gold-band geometry changed (RMB group x=330).  Bump the
        # transport layout revision so an otherwise identical payload is drawn
        # once after the firmware update instead of being skipped as unchanged.
        result["layout_version"] = AI_USAGE_LAYOUT_VERSION
        result["font_version"] = "misans-v1"
    except Exception:  # noqa: BLE001
        logger.debug("[AI_USAGE] feed attach skipped")

    # 额度历史（仅发布端无设备上下文时记录；变化才写，相同则 ≥10min 抽样一次）
    if not mac:
        try:
            from .quota_history_store import record_if_changed

            ds_v = result.get("deepseek_balance")
            cx_pct = None
            cx_label = None
            if result.get("codex_windows"):
                first = result["codex_windows"][0]
                cx_pct = first.get("used_percent")
                cx_label = first.get("label")
            _plan_h = (result.get("codex_meta") or "")
            if "Plus" in _plan_h:
                _plan_h = "plus"
            elif "Pro" in _plan_h:
                _plan_h = "pro"
            else:
                _plan_h = ""
            await record_if_changed(str(ds_v) if ds_v not in (None, "--") else None,
                                    cx_pct, cx_label, _plan_h or None)
        except Exception as e:  # noqa: BLE001
            logger.warning("[AI_USAGE] quota history record failed: %s", e)
    return result


async def _generate_image_gen_content(mode_def: dict, content_cfg: dict, fallback: dict, **kwargs) -> dict:
    provider = content_cfg.get("provider", "")
    if provider == "text2image":
        from .content import generate_artwall_content
        mode_id = str(mode_def.get("mode_id", "") or "").upper()
        mode_display_name = str(mode_def.get("display_name", "") or "")
        mode_description = str(mode_def.get("description", "") or "")
        prompt_hint = str(content_cfg.get("prompt_hint", "") or "")
        prompt_template = str(content_cfg.get("prompt_template", "") or "")
        fallback_title = str(fallback.get("artwork_title", "") or "")
        api_key = kwargs.get("api_key")
        llm_provider = kwargs.get("llm_provider") or DEFAULT_LLM_PROVIDER
        llm_model = kwargs.get("llm_model") or DEFAULT_LLM_MODEL
        try:
            result = await generate_artwall_content(
                date_str=kwargs.get("date_str", ""),
                weather_str=kwargs.get("weather_str", ""),
                festival=kwargs.get("festival", ""),
                colors=int(kwargs.get("colors", 2) or 2),
                llm_provider=llm_provider,
                llm_model=llm_model,
                image_provider=kwargs.get("image_provider") or DEFAULT_IMAGE_PROVIDER,
                image_model=kwargs.get("image_model") or DEFAULT_IMAGE_MODEL,
                mode_display_name=mode_display_name,
                mode_description=mode_description,
                prompt_hint=prompt_hint,
                prompt_template=prompt_template,
                fallback_title=fallback_title,
                image_api_key=kwargs.get("image_api_key"),
                api_key=api_key,
                llm_base_url=kwargs.get("llm_base_url"),
                language=kwargs.get("language", "zh"),
            )
            # 仅当真正拿到图像地址时才使用生成结果；否则回退到 JSON 中的 fallback/fallback_pool
            if mode_id != "ARTWALL":
                result["artwork_title"] = ""
                result["description"] = ""
            if result.get("image_url"):
                # 成功生成图像
                result["_llm_used"] = True
                result["_llm_ok"] = True
                return result
            else:
                # 没有生成图像，使用 fallback
                logger.warning(f"[JSONContent] image_gen for {mode_id} returned no image_url, using fallback")
                fb = dict(fallback)
                fb["_llm_used"] = True
                fb["_llm_ok"] = False
                fb["_used_fallback"] = True
                return fb
        except Exception as e:
            logger.warning(f"[JSONContent] image_gen failed for {mode_id}: {e}", exc_info=True)
            fb = dict(fallback)
            fb["_llm_used"] = True
            fb["_llm_ok"] = False
            fb["_used_fallback"] = True
            return fb
    return dict(fallback)


async def _generate_composite_content(mode_def: dict, content_cfg: dict, fallback: dict, **kwargs) -> dict:
    steps = content_cfg.get("steps", [])
    result: dict[str, Any] = {}
    any_llm_used = False
    any_llm_failed = False
    
    for step in steps:
        try:
            resolved_step = step
            if result and step.get("type") == "llm":
                pt = step.get("prompt_template", "")
                if pt and "{" in pt:
                    import re as _re
                    def _repl(m: _re.Match, _acc=result) -> str:
                        if m.group(1) == "context":
                            return m.group(0)
                        v = _acc.get(m.group(1), "")
                        return str(v) if v else ""
                    resolved_step = {**step, "prompt_template": _re.sub(r"\{(\w+)\}", _repl, pt)}
            step_mode_def = {
                "mode_id": mode_def.get("mode_id", "COMPOSITE"),
                "content": resolved_step,
            }
            part = await generate_json_mode_content(step_mode_def, **kwargs)
            if isinstance(part, dict):
                # 检查这个 step 是否使用了 LLM
                if part.get("_llm_used"):
                    any_llm_used = True
                    if not part.get("_llm_ok", True):
                        any_llm_failed = True
                # 移除内部标记，避免污染最终结果
                part_clean = {k: v for k, v in part.items() if not k.startswith("_")}
                result.update(part_clean)
        except (LLMKeyMissingError, httpx.HTTPError, OSError, TypeError, ValueError, JSONDecodeError) as e:
            logger.warning(f"[JSONContent] Step failed in composite mode {mode_def.get('mode_id', 'UNKNOWN')}: {e}", exc_info=True)
            any_llm_failed = True
            # Continue with next step instead of failing entirely
            continue
    
    if not result:
        fb = dict(fallback)
        if any_llm_used:
            fb["_llm_used"] = True
            fb["_llm_ok"] = False
            fb["_used_fallback"] = True
        return fb
    
    merged = dict(fallback)
    merged.update(result)
    
    # 设置 LLM 使用标记
    if any_llm_used:
        merged["_llm_used"] = True
        if any_llm_failed:
            merged["_llm_ok"] = False
            merged["_used_fallback"] = True
        else:
            merged["_llm_ok"] = True
    
    return merged


def _apply_post_process(result: dict, content_cfg: dict) -> dict:
    """Apply optional post-processing rules to content fields."""
    rules = content_cfg.get("post_process", {})
    for field_name, rule in rules.items():
        val = result.get(field_name, "")
        if not isinstance(val, str):
            continue
        if rule == "first_char":
            result[field_name] = val[:1] if val else ""
        elif rule == "strip_quotes":
            result[field_name] = val.strip('""\u201c\u201d\u300c\u300d')
        elif rule == "recipe_normalize_item_sep":
            s = val.strip()
            parts = [
                p.strip()
                for p in re.split(r"\s*[,，、]\s*|\s*[·・]\s*", s)
                if p and p.strip()
            ]
            result[field_name] = " · ".join(parts) if parts else s
    return result


def _parse_llm_output(text: str, content_cfg: dict, fallback: dict) -> dict:
    """Parse LLM text output according to output_format."""
    fmt = content_cfg.get("output_format", "raw")

    if fmt == "text_split":
        return _parse_text_split(text, content_cfg, fallback)
    elif fmt == "json":
        return _parse_json_output(text, content_cfg, fallback)
    else:
        fields = content_cfg.get("output_fields", ["text"])
        return {fields[0]: text}


def _parse_text_split(text: str, content_cfg: dict, fallback: dict) -> dict:
    """Split text by separator and map to output_fields."""
    sep = content_cfg.get("output_separator", "|")
    fields = content_cfg.get("output_fields", ["text"])
    parts = text.split(sep)

    result = {}
    for i, field_name in enumerate(fields):
        if i < len(parts):
            result[field_name] = parts[i].strip().strip('""\u201c\u201d')
        else:
            result[field_name] = fallback.get(field_name, "")
    return result


def _parse_json_output(text: str, content_cfg: dict, fallback: dict) -> dict:
    """Parse JSON from LLM response."""
    try:
        cleaned = _clean_json_response(text)
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            return dict(fallback)

        fields = content_cfg.get("output_fields")
        if fields:
            return {f: data.get(f, fallback.get(f, "")) for f in fields}
        return data
    except (json.JSONDecodeError, KeyError) as e:
        logger.error("[JSONContent] JSON parse failed: %s | raw[:200]: %s", e, text[:200])
        return dict(fallback)


def _parse_llm_json_output(text: str, content_cfg: dict, fallback: dict) -> dict:
    """Parse JSON from LLM response using output_schema for defaults."""
    schema = content_cfg.get("output_schema", {})
    try:
        cleaned = _clean_json_response(text)
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            logger.warning("[JSONContent] LLM returned non-dict JSON, falling back. Raw: %s", text[:200])
            return dict(fallback)

        result = {}
        for field_name, field_def in schema.items():
            if isinstance(field_def, dict):
                default = field_def.get("default", "")
            else:
                default = field_def
            result[field_name] = data.get(field_name, default)
        return result
    except (json.JSONDecodeError, KeyError) as e:
        logger.error("[JSONContent] LLM JSON parse failed: %s | raw[:200]: %s", e, text[:200])
        return dict(fallback)
