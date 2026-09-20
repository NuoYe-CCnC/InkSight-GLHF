"""Sanitized audit trail for local-console state changes."""
from __future__ import annotations

import copy
import time

from . import state_store

_STATE = state_store.state_path("local_console_audit.json")
_KEEP = 500


def record(event: str, user_id: int, **metadata) -> None:
    safe = {
        key: copy.deepcopy(value)
        for key, value in metadata.items()
        if key in {"task_id", "plan_id", "status", "changed", "device_fingerprint", "target"}
    }

    def update(value: dict) -> None:
        rows = value.setdefault("events", [])
        rows.append({"at": int(time.time()), "event": event, "user_id": int(user_id), **safe})
        value["events"] = rows[-_KEEP:]

    state_store.update_json(_STATE, update, default={"events": []})


def recent(limit: int = 50) -> list[dict]:
    value, error = state_store.read_json(_STATE)
    if error == "missing":
        return []
    if error or not isinstance(value, dict):
        raise state_store.StateStoreError(f"{_STATE.name}: {error or 'invalid'}")
    rows = [row for row in value.get("events", []) if isinstance(row, dict)]
    return copy.deepcopy(rows[-max(1, min(int(limit), 100)):])
