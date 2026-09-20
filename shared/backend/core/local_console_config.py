"""Draft and apply workflow for the local operator console.

The existing operator config remains the only effective source of truth.  A
draft is inert until explicitly applied and is stored privately under the
runtime state directory.
"""
from __future__ import annotations

import copy
import hashlib
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from . import operator_config, state_store

_DRAFT = state_store.state_path("local_console_config_draft.json")
_DRAFT_TTL_SECONDS = 24 * 60 * 60
_DRAFT_LOCK = threading.RLock()
_SECRET_PATHS = {
    "wifi_networks": ("wifi", "networks"),
    "deepseek_api_key": ("services", "deepseek_api_key"),
    "news_deepseek_api_key": ("services", "news_deepseek_api_key"),
    "goldapi_key": ("services", "goldapi_key"),
    "openai_admin_api_key": ("services", "openai_admin_api_key"),
    "cloud_base_url": ("cloud", "base_url"),
    "cloud_user": ("cloud", "user"),
    "cloud_password": ("cloud", "password"),
}


def configure_draft_file(path: Path) -> None:
    global _DRAFT
    _DRAFT = Path(path)


class DraftConflictError(RuntimeError):
    """A draft operation would overwrite work or a newer effective config."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _pair_revision(public: dict, private: dict) -> str:
    """Internal-only digest used to detect public *and* secret config changes."""
    payload = json.dumps(
        {"public": public, "private": private},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_expired(draft: dict, *, now: int | None = None) -> bool:
    updated_at = draft.get("updated_at")
    if not isinstance(updated_at, (int, float)):
        return True
    return int(now if now is not None else time.time()) - int(updated_at) > _DRAFT_TTL_SECONDS


def _new_revision() -> str:
    return secrets.token_urlsafe(24)


def _assert_expected(draft: dict, expected_revision: str | None) -> None:
    if not expected_revision or not secrets.compare_digest(
        str(expected_revision), str(draft.get("revision") or "")
    ):
        raise DraftConflictError(
            "draft_changed",
            "草稿已在另一页面或标签页中更新，请重新载入后再操作。",
        )


def _assert_base_current(draft: dict, effective: operator_config.EffectiveConfig) -> None:
    current = _pair_revision(effective.config, effective._secrets)
    if not secrets.compare_digest(str(draft.get("base_revision") or ""), current):
        raise DraftConflictError(
            "effective_changed",
            "已应用配置在草稿创建后发生变化，请放弃旧草稿并重新载入。",
        )


def _secret_status(config: dict, secrets: dict) -> dict:
    result: dict[str, dict[str, Any]] = {}
    for name, ref in (config.get("secret_refs") or {}).items():
        value: Any = secrets
        for part in str(ref).split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(value, list):
            result[name] = {"configured": bool(value), "count": len(value)}
        else:
            result[name] = {"configured": bool(value)}
    return result


def _changed_secret_fields(before: dict, after: dict) -> list[str]:
    changed: list[str] = []
    for name, path in _SECRET_PATHS.items():
        left: Any = before
        right: Any = after
        for part in path:
            left = left.get(part) if isinstance(left, dict) else None
            right = right.get(part) if isinstance(right, dict) else None
        if left != right:
            changed.append(name)
    return changed


def _load_draft() -> tuple[dict | None, str | None]:
    value, error = state_store.read_json(_DRAFT)
    if error == "missing":
        return None, None
    if error or not isinstance(value, dict):
        return None, error or "invalid"
    if not isinstance(value.get("public"), dict) or not isinstance(value.get("private"), dict):
        return None, "invalid"
    return value, None


def view() -> dict:
    effective = operator_config.load_effective()
    draft, draft_error = _load_draft()
    payload = {
        "effective": copy.deepcopy(effective.config),
        "fingerprint": effective.fingerprint,
        "sources": list(effective.sources),
        "warnings": list(effective.warnings),
        "degraded": effective.degraded,
        "errors": list(effective.errors or []),
        "secret_status": _secret_status(effective.config, effective._secrets),
        "draft": None,
        "draft_error": draft_error,
    }
    if draft:
        expired = _is_expired(draft)
        base_changed = draft.get("base_revision") != _pair_revision(
            effective.config, effective._secrets
        )
        payload["draft"] = {
            "public": copy.deepcopy(draft["public"]),
            "secret_status": _secret_status(draft["public"], draft["private"]),
            "changed_secret_fields": _changed_secret_fields(
                effective._secrets, draft["private"]
            ),
            "base_fingerprint": draft.get("base_fingerprint"),
            "revision": draft.get("revision"),
            "stale": expired or base_changed,
            "expired": expired,
            "base_changed": base_changed,
            "updated_at": draft.get("updated_at"),
        }
    return payload


def _set_secret(target: dict, path: tuple[str, ...], value: Any) -> None:
    cursor = target
    for part in path[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[path[-1]] = copy.deepcopy(value)


def save_draft(
    public: dict,
    secret_updates: dict | None = None,
    *,
    expected_revision: str | None = None,
) -> dict:
    if not isinstance(public, dict):
        raise operator_config.ConfigError(["public: expected object"])
    with _DRAFT_LOCK:
        effective = operator_config.load_effective()
        existing, error = _load_draft()
        if error:
            raise DraftConflictError("draft_invalid", "草稿文件无效，请先放弃草稿后重试。")
        if existing is not None:
            _assert_expected(existing, expected_revision)
            if _is_expired(existing):
                raise DraftConflictError("draft_expired", "草稿已过期，请放弃后重新保存。")
            _assert_base_current(existing, effective)
            candidate_private = copy.deepcopy(existing["private"])
            base_revision = existing["base_revision"]
            base_fingerprint = existing.get("base_fingerprint")
        else:
            if expected_revision:
                raise DraftConflictError("draft_missing", "草稿已被放弃或应用，请重新载入。")
            candidate_private = copy.deepcopy(effective._secrets)
            base_revision = _pair_revision(effective.config, effective._secrets)
            base_fingerprint = effective.fingerprint
        candidate_public = copy.deepcopy(public)
        for name, value in (secret_updates or {}).items():
            path = _SECRET_PATHS.get(name)
            if path is None:
                raise operator_config.ConfigError([f"secret_updates.{name}: unsupported field"])
            _set_secret(candidate_private, path, value)
        errors = operator_config.validate_config(candidate_public)
        errors.extend(operator_config.validate_secrets(candidate_private, enforce_permissions=False))
        if errors:
            raise operator_config.ConfigError(errors)
        state_store.write_json(_DRAFT, {
            "public": candidate_public,
            "private": candidate_private,
            "base_fingerprint": base_fingerprint,
            "base_revision": base_revision,
            "revision": _new_revision(),
        })
        return view()


def discard_draft(*, expected_revision: str | None = None) -> bool:
    with _DRAFT_LOCK:
        draft, error = _load_draft()
        if error:
            raise DraftConflictError("draft_invalid", "草稿文件无效，需要在本机处理后再继续。")
        if draft is None:
            if expected_revision:
                raise DraftConflictError("draft_missing", "草稿已被放弃或应用，请重新载入。")
            return False
        _assert_expected(draft, expected_revision)
        return state_store.remove_json(_DRAFT)


def apply_draft(*, expected_revision: str | None = None) -> dict:
    with _DRAFT_LOCK:
        draft, error = _load_draft()
        if error or draft is None:
            raise DraftConflictError("draft_missing", "草稿不存在或已经被应用。")
        _assert_expected(draft, expected_revision)
        if _is_expired(draft):
            raise DraftConflictError("draft_expired", "草稿已过期，请放弃后重新保存。")
        previous = operator_config.load_effective()
        _assert_base_current(draft, previous)
        previous_key = str(
            (previous._secrets.get("services") or {}).get("news_deepseek_api_key") or ""
        )
        next_key = str(
            (draft["private"].get("services") or {}).get("news_deepseek_api_key") or ""
        )
        previous_openai_key = str(
            (previous._secrets.get("services") or {}).get("openai_admin_api_key") or ""
        )
        next_openai_key = str(
            (draft["private"].get("services") or {}).get("openai_admin_api_key") or ""
        )
        changed = operator_config.atomic_replace_pair(draft["public"], draft["private"])
        try:
            if previous_key != next_key:
                from . import news_schedule
                state_store.write_json(news_schedule.AUTHORIZED_FILE, {
                    "authorized": False,
                    "key_configured": bool(next_key),
                    "key_status": "pending_validation" if next_key else "missing",
                    "reason": "key-changed-requires-validation",
                })
            if previous_openai_key != next_openai_key:
                from . import openai_costs
                openai_costs.mark_key_changed(next_openai_key)
        except Exception:
            operator_config.atomic_replace_pair(previous.config, previous._secrets)
            raise
        state_store.remove_json(_DRAFT)
        result = view()
        result["changed"] = changed
        return result
