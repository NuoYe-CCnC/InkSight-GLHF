"""Durable manual-news operations for the local management console."""
from __future__ import annotations

import copy
import hashlib
import re
import secrets
import threading
import time
from pathlib import Path

from . import news_brief, news_calendar, news_schedule, state_store

_STATE = state_store.state_path("manual_issue_tasks.json")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_ACTIVE = {"queued", "running"}
_THREADS: dict[str, threading.Thread] = {}
_THREADS_LOCK = threading.Lock()
_KEEP = 100


def configure_state_file(path: Path) -> None:
    global _STATE
    _STATE = Path(path)


def _empty() -> dict:
    return {"tasks": {}, "idempotency": {}}


def _read() -> dict:
    value, error = state_store.read_json(_STATE)
    if error == "missing":
        return _empty()
    if error or not isinstance(value, dict):
        raise state_store.StateStoreError(f"{_STATE.name}: {error or 'invalid'}")
    value.setdefault("tasks", {})
    value.setdefault("idempotency", {})
    return value


def _task_public(task: dict) -> dict:
    return copy.deepcopy(task)


def preflight() -> dict:
    cfg = copy.deepcopy(news_schedule._config())
    credential = news_schedule.credential_state()
    current = news_brief._load_state().get("current")
    current_summary = None
    if isinstance(current, dict):
        current_summary = {
            "mode": current.get("mode"),
            "title": current.get("issue_title"),
            "generated_at": current.get("generated_at"),
            "freshness": current.get("freshness"),
            "version": current.get("version"),
        }
    return {
        "credential": credential,
        "generation": {
            "model": cfg.get("model"),
            "max_input_tokens": cfg.get("max_input_tokens"),
            "max_output_tokens": cfg.get("max_output_tokens"),
            "max_calls_per_issue": news_schedule._max_model_calls(cfg),
        },
        "fallback": "daily_message" if credential["state"] == "missing" else "retain_previous_valid",
        "current": current_summary,
        "schedule_unchanged": True,
    }


def create(idempotency_key: str, *, issue_title: str = "手动生成") -> tuple[dict, bool]:
    if not _IDEMPOTENCY_RE.match(idempotency_key or ""):
        raise ValueError("idempotency_key must contain 8-128 safe characters")
    cfg = copy.deepcopy(news_schedule._config())
    now = int(time.time())
    task_id = "issue-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-" + secrets.token_hex(3)

    def update(value: dict) -> tuple[dict, bool]:
        tasks = value.setdefault("tasks", {})
        idempotency = value.setdefault("idempotency", {})
        existing_id = idempotency.get(idempotency_key)
        if existing_id and isinstance(tasks.get(existing_id), dict):
            return _task_public(tasks[existing_id]), False
        active = [row for row in tasks.values() if isinstance(row, dict) and row.get("status") in _ACTIVE]
        if active:
            raise RuntimeError("another manual issue is already active")
        fingerprint = hashlib.sha256(
            repr(sorted(cfg.items())).encode("utf-8")
        ).hexdigest()[:16]
        task = {
            "id": task_id,
            "idempotency_key": idempotency_key,
            "issue_key": "manual|" + task_id,
            "title": (issue_title or "手动生成")[:48],
            "status": "queued",
            "outcome": None,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "updated_at": now,
            "config_fingerprint": fingerprint,
            "issue_config": cfg,
            "credential_at_create": news_schedule.credential_state(),
            "schedule_unchanged": True,
        }
        tasks[task_id] = task
        idempotency[idempotency_key] = task_id
        ordered = sorted(tasks.values(), key=lambda row: int(row.get("created_at") or 0), reverse=True)
        keep_ids = {row["id"] for row in ordered[:_KEEP] if row.get("id")}
        value["tasks"] = {key: row for key, row in tasks.items() if key in keep_ids}
        value["idempotency"] = {
            key: value_id for key, value_id in idempotency.items() if value_id in keep_ids
        }
        return _task_public(task), True

    return state_store.update_json(_STATE, update, default=_empty())


def get(task_id: str) -> dict | None:
    task = (_read().get("tasks") or {}).get(task_id)
    return _task_public(task) if isinstance(task, dict) else None


def list_recent(limit: int = 20) -> list[dict]:
    tasks = [row for row in (_read().get("tasks") or {}).values() if isinstance(row, dict)]
    tasks.sort(key=lambda row: int(row.get("created_at") or 0), reverse=True)
    return [_task_public(row) for row in tasks[:max(1, min(int(limit), 100))]]


def _set_status(task_id: str, status: str, **fields) -> dict:
    def update(value: dict) -> dict:
        task = (value.setdefault("tasks", {})).get(task_id)
        if not isinstance(task, dict):
            raise KeyError(task_id)
        task["status"] = status
        task["updated_at"] = int(time.time())
        task.update(copy.deepcopy(fields))
        return _task_public(task)

    return state_store.update_json(_STATE, update, default=_empty())


def run(task_id: str) -> dict:
    task = get(task_id)
    if not task:
        raise KeyError(task_id)
    if task.get("status") == "completed":
        return task
    if task.get("status") not in {"queued", "running", "failed", "uncertain"}:
        raise RuntimeError("task cannot be resumed")
    _set_status(task_id, "running", started_at=task.get("started_at") or int(time.time()))
    try:
        outcome = news_schedule.run_manual_issue(
            str(task["issue_key"]),
            copy.deepcopy(task["issue_config"]),
            now_dt=news_calendar.bj_now(),
            issue_title=str(task.get("title") or "手动生成"),
        )
        completed = outcome.startswith("published") or outcome.startswith("skip:keep-old")
        return _set_status(
            task_id,
            "completed" if completed else "failed",
            outcome=outcome,
            finished_at=int(time.time()),
        )
    except Exception as exc:
        return _set_status(
            task_id,
            "uncertain",
            outcome="uncertain:worker-error",
            error_type=type(exc).__name__,
            finished_at=int(time.time()),
        )


def start(task_id: str) -> dict:
    task = get(task_id)
    if not task:
        raise KeyError(task_id)
    if task.get("status") == "completed":
        return task
    with _THREADS_LOCK:
        existing = _THREADS.get(task_id)
        if existing and existing.is_alive():
            return task
        worker = threading.Thread(target=run, args=(task_id,), daemon=True, name=f"inksight-{task_id}")
        _THREADS[task_id] = worker
        worker.start()
    return get(task_id) or task
