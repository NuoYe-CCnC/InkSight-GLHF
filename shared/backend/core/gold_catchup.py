"""Cold-start-only, stale-only XAUS recovery coordinator.

The device may request recovery only after a true power-on when its published
quote is missing or older than 24 hours. This host independently re-checks the
authoritative cache and applies a durable one-hour cooldown, so a faulty or old
device cannot turn frequent deep-sleep wakes into provider traffic.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import gold_feed as gf

logger = logging.getLogger(__name__)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,95}$")
COOLDOWN_S = 3600
DAILY_BUDGET = 4
STALE_AFTER_S = 86400
_BJ = timezone(timedelta(hours=8))


def _safe_request_id(value: str | None) -> str:
    text = str(value or "").strip()
    if not _REQUEST_ID_RE.fullmatch(text):
        raise ValueError("invalid request_id")
    return text


def request_fingerprint(request_id: str) -> str:
    """Loggable identifier which does not expose a device MAC or token."""
    return hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:12]


def needs_catchup(now: Optional[float] = None) -> bool:
    """Re-check age on the host; never trust the device's stale claim."""
    current = float(time.time() if now is None else now)
    item = gf.cached(now=current)
    if not item:
        return True
    quote_time = item.get("quote_time")
    if not isinstance(quote_time, (int, float)):
        return True
    return current - float(quote_time) > STALE_AFTER_S


def _state() -> dict:
    state = gf._load_state()
    state.setdefault("catchup", {})
    return state


def _save(state: dict) -> None:
    gf._save_state(state)


def can_attempt(now: Optional[float] = None) -> tuple[bool, str]:
    current = float(time.time() if now is None else now)
    state = _state()
    recovery = state.get("catchup") or {}
    last_attempt = float(recovery.get("last_attempt_at") or 0)
    if 0 <= current - last_attempt < COOLDOWN_S:
        return False, f"cooldown (last {int(current - last_attempt)}s ago)"
    day = datetime.fromtimestamp(current, _BJ).strftime("%Y-%m-%d")
    if recovery.get("day") != day:
        recovery["day"] = day
        recovery["count"] = 0
        state["catchup"] = recovery
        _save(state)
    if int(recovery.get("count") or 0) >= DAILY_BUDGET:
        return False, f"daily budget exhausted ({DAILY_BUDGET})"
    return True, "ok"


def _record_attempt(current: float, ok: bool, note: str) -> None:
    state = _state()
    recovery = state.setdefault("catchup", {})
    day = datetime.fromtimestamp(current, _BJ).strftime("%Y-%m-%d")
    if recovery.get("day") != day:
        recovery["day"] = day
        recovery["count"] = 0
    recovery["last_attempt_at"] = current
    if ok:
        recovery["count"] = int(recovery.get("count") or 0) + 1
        recovery["last_success_at"] = current
    recovery["last_result"] = {"ok": ok, "note": note, "at": current}
    _save(state)


def catchup(now: Optional[float] = None, request_id: str | None = None,
            reason: str = "device_wake") -> dict:
    rid = _safe_request_id(request_id) if request_id else None
    current = float(time.time() if now is None else now)
    if not needs_catchup(current):
        return {"ok": True, "action": "noop", "reason": "quote fresh (<=24h)",
                "item": gf.cached(now=current)}
    allowed, why = can_attempt(current)
    if not allowed:
        return {"ok": False, "action": "blocked", "reason": why,
                "item": gf.cached(now=current)}
    result = gf.refresh(force=True, reason=reason, request_id=rid, now=current)
    _record_attempt(current, bool(result.get("ok")),
                    "catchup ok" if result.get("ok") else str(result.get("note") or result))
    logger.info("[GOLD] cold-start catchup request=%s result=%s",
                request_fingerprint(rid) if rid else "admin", _result_word(result))
    if result.get("ok") and not result.get("deduped"):
        try:
            from .feed_document import build_feed_document
            build_feed_document(write_file=True)
        except Exception:  # noqa: BLE001
            logger.warning("[GOLD] feed rebuild after wake refresh failed")
    return {**result, "action": ("coalesced" if result.get("coalesced") else
                                  "deduped" if result.get("deduped") else
                                  "fetched" if result.get("ok") else "failed")}


def _result_word(result: dict) -> str:
    if result.get("deduped"):
        return "deduped"
    if result.get("coalesced"):
        return "coalesced"
    return "ok" if result.get("ok") else str(result.get("error_class") or "failed")


def status() -> dict:
    value = gf.status()
    recovery = _state().get("catchup") or {}
    value.update({"wake_refresh": True, "wake_refresh_policy": "cold-start-stale-only",
                  "needs_catchup": needs_catchup(),
                  "request_id_required_for_device": True,
                  "cold_start_cooldown_seconds": COOLDOWN_S,
                  "cold_start_daily_count": recovery.get("count", 0),
                  "cold_start_daily_budget": DAILY_BUDGET,
                  "minimum_provider_interval_seconds": gf._minimum_interval()})
    return value
