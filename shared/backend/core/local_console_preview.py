"""Side-effect-free 800 x 480 previews for the loopback operator console.

This module deliberately does not call the generic preview pipeline.  It reads
only already persisted values and renders the same document format used by the
publisher/firmware acceptance renderer.  Consequently opening the console can
never trigger an account balance lookup, market refresh, publication, or model
request.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import os
import time
from pathlib import Path
from typing import Any

from PIL import Image

from . import data_cache, gold_feed, news_brief, openai_costs, operator_config, reliable_sources
from .codex_usage_store import get_codex_usage
from .config_store import get_user_devices
from .date_table import build_table_payload
from .deepseek_token_tracker import snapshot as token_snapshot

_ROOT = Path(__file__).resolve().parents[3]
_RENDERER_PATH = _ROOT / "shared" / "tools" / "misans_panel_render.py"
_DEVICE_ID_SECRET = os.urandom(32)
_RENDERER = None


def _renderer():
    global _RENDERER
    if _RENDERER is None:
        spec = importlib.util.spec_from_file_location("inksight_misans_panel_render", _RENDERER_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("panel preview renderer is unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _RENDERER = module
    return _RENDERER


def _device_id(user_id: int, mac: str) -> str:
    body = f"{user_id}:{mac.upper()}".encode("utf-8")
    return "screen-" + hmac.new(_DEVICE_ID_SECRET, body, hashlib.sha256).hexdigest()[:18]


async def list_devices(user_id: int) -> list[dict]:
    rows = await get_user_devices(user_id)
    return [
        {
            "id": _device_id(user_id, str(row["mac"])),
            "label": str(row.get("nickname") or "已绑定设备"),
            "last_seen": row.get("last_seen"),
        }
        for row in rows
        if row.get("mac")
    ]


async def _resolve_device(user_id: int, opaque_id: str | None) -> str | None:
    if not opaque_id:
        return None
    rows = await get_user_devices(user_id)
    for row in rows:
        mac = str(row.get("mac") or "").upper()
        if mac and hmac.compare_digest(_device_id(user_id, mac), opaque_id):
            return mac
    raise ValueError("preview device is not available to this user")


def _seven_day(codex: dict | None) -> tuple[float | None, int | None, int | None]:
    if not isinstance(codex, dict):
        return None, None, None
    for row in codex.get("windows") or []:
        if not isinstance(row, dict) or int(row.get("duration_minutes") or 0) != 10080:
            continue
        try:
            used = float(row.get("used_percent"))
        except (TypeError, ValueError):
            return None, None, None
        return used if 0 <= used <= 100 else None, row.get("resets_at"), int(codex.get("ts") or 0)
    return None, None, int(codex.get("ts") or 0)


def _cached_ai(codex: dict | None, *, selected: bool) -> dict:
    config = operator_config.load_effective().config
    display = dict(config.get("panel_display") or {})
    member = dict(config.get("codex_member") or {}) if selected else {}
    used, resets_at, codex_ts = _seven_day(codex if selected else None)
    ds = reliable_sources.ds_values_cached() if selected else {"CNY": None, "USD": None}
    tokens = token_snapshot() if selected else {"tokens": None, "complete": False, "source": "unbound"}
    codex_group = data_cache.get_group("ai.codex") or {}
    codex_value = codex_group.get("value") if isinstance(codex_group.get("value"), dict) else {}
    result: dict[str, Any] = {
        "codex_7d_used": used,
        "codex_resets_at": resets_at,
        "reset_credits_available": codex_value.get("reset_credits_available") if selected else None,
        "reset_credits_confirmed": bool(selected and codex_group.get("status") == "fresh"),
        "reset_expiry_list": codex_value.get("reset_expiry_list") if selected else None,
        "plan": member.get("plan"),
        "valid_until_date": member.get("valid_until_date"),
        "deepseek": {"CNY": ds.get("CNY"), "USD": ds.get("USD")},
        "deepseek_today_tokens": tokens.get("tokens"),
        "deepseek_today_tokens_complete": bool(tokens.get("complete")),
        "show_codex_credits": bool(display.get("show_codex_credits", False)),
        "show_openai_api_info": bool(display.get("show_openai_api_info", False)),
        "show_reset_opportunities": bool(display.get("show_reset_opportunities", True)),
        "fresh": {
            "codex": codex_ts or 0,
            "ds_cny": int((data_cache.get_group("ai.ds.cny") or {}).get("last_success") or 0) if selected else 0,
            "ds_usd": int((data_cache.get_group("ai.ds.usd") or {}).get("last_success") or 0) if selected else 0,
            "member": 0,
        },
    }
    if selected and isinstance(codex, dict):
        result["codex_credit_balance"] = codex.get("credit_balance")
        result["codex_credit_has_credits"] = codex.get("credit_has_credits")
        result["codex_credit_unlimited"] = codex.get("credit_unlimited")
        result["codex_account_key"] = codex.get("account_key")
    cost = openai_costs.snapshot() if selected else {
        "complete": False, "amount": None, "state": "unbound", "month": None,
    }
    result.update({
        "openai_api_month_spend_usd": (cost.get("amount")
                                         if selected and cost.get("complete") is True else None),
        "openai_api_month_spend_complete": bool(selected and cost.get("complete") is True),
        "openai_api_month_spend_state": cost.get("state") if selected else "unbound",
        "openai_api_month": cost.get("month"),
        "openai_api_month_basis": "UTC",
    })
    return result


async def build_document(user_id: int, device_id: str | None = None) -> tuple[dict, dict]:
    mac = await _resolve_device(user_id, device_id)
    codex = await get_codex_usage(mac) if mac else None
    ai = _cached_ai(codex, selected=bool(mac))
    current = (news_brief._load_state().get("current") or {}) if mac else {}
    news = {key: current.get(key) for key in (
        "mode", "period", "issue_title", "date", "planned_at", "generated_at",
        "text", "freshness", "origin", "version",
    )} if isinstance(current, dict) else {}
    gold = gold_feed.cached() if mac else None
    stamps = [int(value or 0) for value in (
        (codex or {}).get("ts") if isinstance(codex, dict) else 0,
        news.get("generated_at") if isinstance(news, dict) else 0,
        (gold or {}).get("price_as_of") if isinstance(gold, dict) else 0,
    )]
    preferences = operator_config.load_effective().config.get("panel_display") or {}
    document = {
        "schema": "inksight_screen_v2",
        "ts": max(stamps),
        "screen": {
            "calendar": build_table_payload(preferences=preferences),
            "pages": {
                "ai": ai,
                "news_gold": {"news": news, "gold": gold or {}},
            },
        },
    }
    return document, {
        "device_bound": bool(mac),
        "device_id": device_id if mac else None,
        "data_policy": "persisted-cache-only",
        "generated_at": int(time.time()),
    }


async def render(user_id: int, *, device_id: str | None = None,
                 page: str = "ai") -> tuple[Image.Image, dict]:
    if page not in {"ai", "news_gold"}:
        raise ValueError("unsupported preview page")
    document, metadata = await build_document(user_id, device_id)
    renderer = _renderer()
    panel = renderer.Panel()
    fonts = renderer.load_fonts()
    now = int(time.time())
    if page == "ai":
        renderer.render_ai_panel(panel, fonts, document, now)
    else:
        renderer.render_news_gold_panel(panel, fonts, document, now)
    if panel.missing:
        raise RuntimeError("preview contains unsupported glyphs")
    image = Image.new("1", (renderer.W, renderer.H), 1)
    pixels = image.load()
    raw = bytes(panel.fb)
    for y in range(renderer.H):
        base = y * renderer.ROW_BYTES
        for x in range(renderer.W):
            if not (raw[base + x // 8] & (0x80 >> (x % 8))):
                pixels[x, y] = 0
    metadata["page"] = page
    metadata["document_timestamp"] = int(document.get("ts") or 0)
    metadata["fields"] = panel.ops
    return image, metadata
