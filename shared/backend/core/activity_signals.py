"""Stable phase-3 semantic and visual keys for the dual-page device.

The activity key deliberately excludes collection timestamps, freshness/status,
news, gold and payload identifiers.  It is valid only when every required AI
field is comparable.  This lets the device distinguish "same value observed
again" from a real AI-content change without treating missing data as zero.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _number(value: Any, digits: int) -> str | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return f"{out:.{digits}f}"


def _epoch(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        out = int(float(value))
    except (OverflowError, TypeError, ValueError):
        return None
    return out if out > 0 else None


def _expiry_list(value: Any) -> list[int] | None:
    if not isinstance(value, list):
        return None
    out: set[int] = set()
    for item in value:
        parsed = _epoch(item)
        if parsed is None:
            return None
        out.add(parsed)
    return sorted(out)


def activity_snapshot(ai: dict | None) -> dict | None:
    """Return the normalized comparable AI snapshot, or ``None`` if unknown."""
    if not isinstance(ai, dict):
        return None
    deepseek = ai.get("deepseek")
    member = ai.get("member")
    if not isinstance(deepseek, dict) or not isinstance(member, dict):
        return None
    used = _number(ai.get("codex_7d_used"), 2)
    if used is None or not (0.0 <= float(used) <= 100.0):
        return None
    resets_at = _epoch(ai.get("codex_resets_at"))
    credits = ai.get("reset_credits_available")
    if isinstance(credits, bool) or not isinstance(credits, (int, float)):
        return None
    credits_i = int(credits)
    if credits_i < 0 or float(credits) != credits_i:
        return None
    expiry = _expiry_list(ai.get("reset_expiry_list"))
    cny = _number(deepseek.get("CNY"), 2)
    usd = _number(deepseek.get("USD"), 2)
    if resets_at is None or expiry is None or cny is None or usd is None:
        return None
    plan = str(member.get("plan") or ai.get("plan") or "").strip().upper()
    valid_until = str(member.get("valid_until_date") or ai.get("valid_until_date") or "").strip()
    if not plan or not valid_until:
        return None
    return {
        "codex_used_pct": used,
        "codex_resets_at": resets_at,
        "reset_credits_available": credits_i,
        "reset_expiry_list": expiry,
        "deepseek_cny": cny,
        "deepseek_usd": usd,
        "member": {
            "plan": plan,
            "valid_until_date": valid_until,
            "renewal_status": str(member.get("renewal_status") or "").strip().lower(),
            "precision": str(member.get("precision") or "").strip().lower(),
        },
    }


def activity_observed_at(ai: dict | None) -> int | None:
    """Oldest required live-source success time; repeated cached data stays old."""
    if activity_snapshot(ai) is None:
        return None
    fresh = (ai or {}).get("fresh")
    if not isinstance(fresh, dict):
        return None
    values = [_epoch(fresh.get(k)) for k in ("codex", "ds_cny", "ds_usd")]
    if any(v is None for v in values):
        return None
    return min(v for v in values if v is not None)


def activity_key(ai: dict | None) -> str | None:
    snap = activity_snapshot(ai)
    return _digest(snap) if snap is not None else None


def ai_visual_key(ai: dict | None) -> str:
    """Fields visible on the AI page; timestamps/status are intentionally absent."""
    ai = ai if isinstance(ai, dict) else {}
    ds = ai.get("deepseek") if isinstance(ai.get("deepseek"), dict) else {}
    prefs = ai.get("display_preferences") if isinstance(ai.get("display_preferences"), dict) else {}
    show_codex = bool(prefs.get("show_codex_credits", False))
    show_api = bool(prefs.get("show_openai_api_info", False))
    credits = ai.get("reset_credits_available")
    try:
        credits_number = float(credits)
    except (OverflowError, TypeError, ValueError):
        credits_number = math.nan
    explicit_zero = (not isinstance(credits, bool)
                     and isinstance(credits, (int, float))
                     and math.isfinite(credits_number)
                     and credits_number == 0.0)
    reset_confirmed = ai.get("reset_credits_confirmed") is True
    lower_balances = explicit_zero and reset_confirmed and not show_codex and not show_api
    visible = {
        "used": _number(ai.get("codex_7d_used"), 2),
        "resets_at": _epoch(ai.get("codex_resets_at")),
        "credits": ai.get("reset_credits_available"),
        "reset_credits_confirmed": reset_confirmed,
        "expiry": _expiry_list(ai.get("reset_expiry_list")),
        "plan": str(ai.get("plan") or "").strip().upper(),
        "valid_until": str(ai.get("valid_until_date") or "").strip(),
        "cny": _number(ds.get("CNY"), 2),
        "usd": _number(ds.get("USD"), 2),
        "today_tokens": ai.get("deepseek_today_tokens"),
        "today_tokens_complete": ai.get("deepseek_today_tokens_complete"),
        "display_preferences": {
            "show_codex_credits": show_codex,
            "show_openai_api_info": show_api,
            # In zero-reset fallback mode this switch changes no pixels, so it
            # must not trigger an unnecessary e-paper refresh.
            "show_reset_opportunities": (bool(prefs.get("show_reset_opportunities", True))
                                           if not lower_balances else None),
        },
        "account_layout": "lower-balances" if lower_balances else "reset",
        "codex_credit_balance": (str(ai.get("codex_credit_balance"))
                                  if (show_codex or lower_balances)
                                  and ai.get("codex_credit_balance") is not None else None),
        "codex_credit_unlimited": (ai.get("codex_credit_unlimited")
                                    if show_codex or lower_balances else None),
        "openai_api_month_spend_usd": (_number(ai.get("openai_api_month_spend_usd"), 2)
                                        if show_api or lower_balances else None),
    }
    return _digest(visible)


def news_gold_visual_key(ai: dict | None, news_gold: dict | None) -> str:
    """Fields visible on page 1, including its compact AI summary."""
    ai = ai if isinstance(ai, dict) else {}
    ds = ai.get("deepseek") if isinstance(ai.get("deepseek"), dict) else {}
    ng = news_gold if isinstance(news_gold, dict) else {}
    news = ng.get("news") if isinstance(ng.get("news"), dict) else {}
    gold = ng.get("gold") if isinstance(ng.get("gold"), dict) else {}
    visible = {
        "ai": {
            "used": _number(ai.get("codex_7d_used"), 2),
            "resets_at": _epoch(ai.get("codex_resets_at")),
            "cny": _number(ds.get("CNY"), 2),
            "usd": _number(ds.get("USD"), 2),
            "today_tokens": ai.get("deepseek_today_tokens"),
            "today_tokens_complete": ai.get("deepseek_today_tokens_complete"),
        },
        "news": {
            "mode": news.get("mode"), "period": news.get("period"),
            "schedule_id": news.get("schedule_id"), "issue_title": news.get("issue_title"),
            "generated_at": _epoch(news.get("generated_at")),
            "text": news.get("text"), "items": news.get("items"),
            "general": news.get("general"), "tech_ai": news.get("tech_ai"),
            "finance": news.get("finance"),
            "update_state": news.get("update_state"),
            "expected_schedule_id": news.get("expected_schedule_id"),
        },
        "gold": {
            "spot_usd_oz": _number(gold.get("spot_usd_oz"), 2),
            "price": _number(gold.get("price_gram_cny"), 2),
            "fx_rate": _number(gold.get("fx_rate"), 2),
            "baseline_date": gold.get("baseline_date"),
            "baseline_method": gold.get("baseline_method"),
            "baseline_price_as_of": _epoch(gold.get("baseline_price_as_of")),
            "baseline_cny_kind": gold.get("baseline_cny_kind"),
            "baseline_cny_price_as_of": _epoch(gold.get("baseline_cny_price_as_of")),
            "change_usd_oz": _number(gold.get("change_usd_oz_since_bj_midnight"), 2),
            "change_cny_g": _number(gold.get("change_cny_g_since_reference",
                                              gold.get("change_cny_g_since_bj_midnight")), 2),
            "change_status": gold.get("change_status"),
            "price_source": gold.get("price_source"),
            "price_as_of": _epoch(gold.get("price_as_of") or gold.get("quote_time")),
            "data_state": (gold.get("data_state") or {}).get("status")
                if isinstance(gold.get("data_state"), dict) else None,
            "stale": bool(gold.get("stale")), "fx_source": gold.get("fx_source"),
            "fx_stale": bool(gold.get("fx_stale")), "status": gold.get("status"),
        },
    }
    return _digest(visible)
