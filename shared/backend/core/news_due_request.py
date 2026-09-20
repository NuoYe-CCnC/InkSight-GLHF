"""Idempotent device/host requests to check the publisher's current due issue.

The caller supplies only a replay-safe request id.  The publisher chooses the
date and schedule from its own Beijing clock, workday calendar and effective
configuration, so a device cannot request historical or arbitrary paid runs.
"""
from __future__ import annotations

import copy
import re
import time
from pathlib import Path

from . import news_schedule, state_store

STATE_FILE = state_store.state_path("news_due_requests.json")
_REQUEST_RE = re.compile(r"^news-[0-9]{8}-[a-z0-9][a-z0-9_-]{0,31}$")
_KEEP = 256


def configure_state_file(path) -> None:
    global STATE_FILE
    STATE_FILE = Path(path)


def _default() -> dict:
    return {"schema": 1, "requests": {}}


def _load() -> dict:
    value, error = state_store.read_json(STATE_FILE)
    if isinstance(value, dict) and not error and value.get("schema") == 1:
        value.setdefault("requests", {})
        return value
    if error == "missing":
        return _default()
    raise state_store.StateStoreError("news due request state unavailable")


def _safe_request_id(value: str | None) -> str:
    request_id = str(value or "")
    if not _REQUEST_RE.fullmatch(request_id):
        raise ValueError("invalid request_id")
    return request_id


def _prune(requests: dict) -> dict:
    rows = sorted(requests.items(), key=lambda item: float((item[1] or {}).get("at") or 0))
    return dict(rows[-_KEEP:])


def check(request_id: str | None, *, now_dt=None, reason: str = "device") -> dict:
    """Check/run only the latest issue currently due under trusted policy."""
    rid = _safe_request_id(request_id)
    with state_store.file_lock(STATE_FILE, operation="news-due-request"):
        state = _load()
        previous = (state.get("requests") or {}).get(rid)
        if isinstance(previous, dict) and previous.get("status") == "done":
            return {"ok": True, "deduped": True, **copy.deepcopy(previous.get("result") or {})}

        before = news_schedule.current_due_status(now_dt)
        if before.get("state") == "due":
            tick = news_schedule.tick(now_dt)
        else:
            tick = None
        after = news_schedule.current_due_status(now_dt)
        action = {
            "not-due": "noop-not-due",
            "current": "already-current",
            "due": "pending",
            "waiting": "waiting-for-credential",
            "generating": "already-generating",
            "failed": "not-updated",
            "expired": "outside-window",
        }.get(str(after.get("state") or ""), "unknown")
        result = {
            "action": action,
            "issue_key": after.get("issue_key"),
            "schedule_id": after.get("schedule_id"),
            "current": bool(after.get("current")),
            "done": after.get("done"),
            "model_calls": after.get("model_calls"),
            "max_model_calls": after.get("max_model_calls"),
            "tick_done": ((tick or {}).get("latest") or {}).get("done") if tick else None,
        }
        state.setdefault("requests", {})[rid] = {
            "at": int(time.time()), "reason": str(reason or "device")[:32],
            "status": "done", "result": result,
        }
        state["requests"] = _prune(state["requests"])
        state_store.write_json(STATE_FILE, state)
        return {"ok": True, "deduped": False, **result}


def status() -> dict:
    try:
        state = _load()
    except state_store.StateStoreError:
        return {"state": "unavailable"}
    requests = state.get("requests") or {}
    latest = max(requests.values(), key=lambda row: float((row or {}).get("at") or 0),
                 default=None)
    return {"state": "ready", "request_count": len(requests), "latest": latest,
            "current_due": news_schedule.current_due_status()}
