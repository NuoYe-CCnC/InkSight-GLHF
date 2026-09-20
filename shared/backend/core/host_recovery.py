"""Bounded host startup/resume recovery for current gold/news state.

The host cannot run while macOS is asleep.  A backend start or native macOS
wake notification therefore performs one immediate, durable recovery event:
one XAUS refresh and one idempotent news-schedule tick.  Repeated events within
15 minutes are suppressed before any network call; provider locking and the
existing 30-second minimum interval still coalesce a simultaneous :00/:30 job.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import state_store

logger = logging.getLogger(__name__)
STATE_FILE = state_store.state_path("host_recovery_state.json")
EVENT_COOLDOWN_S = 15 * 60
_KEEP = 96
_BJ = timezone(timedelta(hours=8))
_REASONS = {"service-start", "host-wake"}


def configure_state_file(path) -> None:
    global STATE_FILE
    STATE_FILE = Path(path)


def _default() -> dict:
    return {"schema": 1, "last_event_at": 0.0, "events": []}


def _reserve(reason: str, now: float) -> tuple[bool, str]:
    event_id = datetime.fromtimestamp(now, _BJ).strftime("%Y%m%dT%H%M%S") + "-" + reason

    def update(value: dict):
        if value.get("schema") != 1:
            raise state_store.StateStoreError("host recovery state schema mismatch")
        last = float(value.get("last_event_at") or 0)
        if 0 <= now - last < EVENT_COOLDOWN_S:
            return False
        value["last_event_at"] = now
        value["last_reason"] = reason
        value.setdefault("events", []).append({
            "id": event_id, "at": now, "reason": reason, "status": "reserved"
        })
        value["events"] = value["events"][-_KEEP:]
        return True

    reserved = state_store.update_json(STATE_FILE, update, default=_default())
    return bool(reserved), event_id


def _finish(event_id: str, result: dict) -> None:
    def update(value: dict):
        for row in reversed(value.get("events") or []):
            if row.get("id") == event_id:
                row.update(result)
                row["status"] = "done"
                break
    state_store.update_json(STATE_FILE, update, default=_default())


def recover(reason: str, *, now: float | None = None) -> dict:
    if reason not in _REASONS:
        raise ValueError("unsupported host recovery reason")
    current = float(time.time() if now is None else now)
    try:
        reserved, event_id = _reserve(reason, current)
    except state_store.StateStoreError as exc:
        logger.error("[RECOVERY] state unavailable; no external calls: %s", exc)
        return {"ok": False, "reason": "state-unavailable", "detail": str(exc)}
    if not reserved:
        return {"ok": True, "deduped": True, "reason": "event-cooldown"}

    gold_summary = {"ok": False, "action": "failed"}
    news_summary = None
    try:
        from .gold_feed import refresh as gold_refresh
        request_id = "host-recovery-" + datetime.fromtimestamp(current, _BJ).strftime("%Y%m%dT%H%M")
        # Normal refresh deduplication owns the current :00/:30 slot. This
        # catches a slot missed during sleep without spending an extra provider
        # request when the latest slot is already present.
        gold = gold_refresh(force=False, reason=reason, request_id=request_id, now=current)
        action = ("current" if gold.get("window_done") else
                  "coalesced" if gold.get("coalesced") else
                  "deduped" if gold.get("deduped") else
                  "fetched" if gold.get("ok") else "failed")
        gold_summary = {"ok": bool(gold.get("ok") or gold.get("window_done")), "action": action,
                        "status": gold.get("status")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[RECOVERY] gold failed: %s", type(exc).__name__)

    try:
        from . import news_due_request, news_schedule
        now_dt = datetime.fromtimestamp(current, _BJ).replace(tzinfo=None)
        due = news_schedule.current_due_status(now_dt)
        if due.get("due"):
            day = now_dt.strftime("%Y%m%d")
            schedule_id = str(due.get("schedule_id") or "current")
            news = news_due_request.check(
                f"news-{day}-{schedule_id}", now_dt=now_dt, reason=reason)
            news_summary = {"ok": True, "done": news.get("done"),
                            "period": news.get("schedule_id"),
                            "action": news.get("action")}
        else:
            news_summary = {"ok": True, "done": None, "period": None,
                            "action": "noop-not-due"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[RECOVERY] news tick failed: %s", type(exc).__name__)
        news_summary = {"ok": False, "done": None, "period": None}

    if gold_summary.get("ok") or (news_summary or {}).get("done"):
        try:
            from .feed_document import build_feed_document
            build_feed_document(write_file=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[RECOVERY] feed rebuild failed: %s", type(exc).__name__)
    result = {"gold": gold_summary, "news": news_summary}
    _finish(event_id, result)
    logger.info("[RECOVERY] reason=%s gold=%s news=%s", reason,
                gold_summary.get("action"), (news_summary or {}).get("done"))
    return {"ok": bool(gold_summary.get("ok") or (news_summary or {}).get("ok")),
            "deduped": False, **result}


def status() -> dict:
    value, error = state_store.read_json(STATE_FILE)
    if error or not isinstance(value, dict):
        return {"state": "missing" if error == "missing" else "unavailable",
                "cooldown_seconds": EVENT_COOLDOWN_S}
    return {"state": "ready", "cooldown_seconds": EVENT_COOLDOWN_S,
            "last_event_at": value.get("last_event_at"),
            "last_reason": value.get("last_reason"),
            "last_event": (value.get("events") or [None])[-1]}
