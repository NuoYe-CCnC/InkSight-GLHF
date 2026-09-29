"""Local-only Codex collector diagnostics; never part of a device payload."""
from __future__ import annotations

import time

from . import state_store


STATE_FILE = state_store.state_path("codex_collector_health.json")
REASONS = {"cli_missing", "cli_start", "rpc_unavailable", "rpc_error",
           "missing_7d", "push_failed", "unexpected"}


def _read() -> dict:
    value, error = state_store.read_json(STATE_FILE)
    if error == "missing":
        return {}
    if error or not isinstance(value, dict):
        return {"_state_unavailable": True}
    return value


def retry_seconds(failures: int) -> int:
    """Bound failed attempts to at most one every ten minutes."""
    return min(600, 60 * (2 ** min(max(0, failures - 1), 4)))


def should_attempt(*, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else int(now)
    state = _read()
    return now >= int(state.get("next_retry_at") or 0)


def record(success: bool, reason: str | None = None, *, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else int(now)
    safe_reason = None if success else reason if reason in REASONS else "unexpected"

    def change(state: dict) -> dict:
        failures = 0 if success else min(1000, int(state.get("consecutive_failures") or 0) + 1)
        state.update({
            "schema": 1,
            "last_attempt_at": now,
            "last_success_at": now if success else state.get("last_success_at"),
            "consecutive_failures": failures,
            "reason": safe_reason,
            "next_retry_at": 0 if success else now + retry_seconds(failures),
        })
        return dict(state)

    return state_store.update_json(STATE_FILE, change, default={})


def snapshot(*, now: int | None = None, stale_after: int = 7200) -> dict:
    now = int(time.time()) if now is None else int(now)
    state = _read()
    success_at = int(state.get("last_success_at") or 0)
    attempt_at = int(state.get("last_attempt_at") or 0)
    if state.get("_state_unavailable"):
        status = "unavailable"
    elif not attempt_at:
        status = "not_started"
    elif not success_at or now - success_at > stale_after:
        status = "stale"
    elif state.get("consecutive_failures"):
        status = "retrying"
    else:
        status = "current"
    return {
        "status": status,
        "last_attempt_at": attempt_at or None,
        "last_success_at": success_at or None,
        "consecutive_failures": int(state.get("consecutive_failures") or 0),
        "reason": state.get("reason") if state.get("reason") in REASONS else None,
        "next_retry_at": int(state.get("next_retry_at") or 0) or None,
    }
