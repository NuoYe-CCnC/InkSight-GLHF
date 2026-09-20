"""Compatibility facade for the versioned InkSight operator configuration.

New code is backed by :mod:`operator_config`.  An explicit ``path=`` keeps the
old raw-file behavior for isolated tests and legacy tooling; normal runtime
calls use the validated effective configuration and separate secret store.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import operator_config


SHARED_ROOT = Path(__file__).resolve().parent.parent.parent
SETTINGS_FILE = operator_config.LEGACY_FILE

_ENV_KEYS = {
    "deepseek_api_key": "DEEPSEEK_API_KEY",
    "news_deepseek_api_key": "NEWS_DEEPSEEK_API_KEY",
    "goldapi_key": "GOLDAPI_KEY",
}


def _raw(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load(path: Path | None = None) -> dict:
    if path is not None:
        return _raw(Path(path))
    return operator_config.load_effective().config


def services(path: Path | None = None) -> dict:
    if path is not None:
        value = _raw(Path(path)).get("services")
        return dict(value) if isinstance(value, dict) else {}
    return {name: operator_config.secret_value(name) for name in _ENV_KEYS}


def member(path: Path | None = None) -> dict:
    value = (load(path).get("codex_member") if path is not None
             else operator_config.load_effective().config.get("codex_member"))
    return dict(value) if isinstance(value, dict) else {}


def panel_display(path: Path | None = None) -> dict:
    value = (load(path).get("panel_display") if path is not None
             else operator_config.load_effective().config.get("panel_display"))
    return dict(value) if isinstance(value, dict) else {}


def token_tracking(path: Path | None = None) -> dict:
    value = (load(path).get("deepseek_token_tracking") if path is not None
             else operator_config.load_effective().config.get("deepseek_token_tracking"))
    return dict(value) if isinstance(value, dict) else {}


def wifi_networks(path: Path | None = None) -> list[dict]:
    if path is not None:
        value = (_raw(Path(path)).get("wifi") or {}).get("networks")
        return [dict(row) for row in value] if isinstance(value, list) else []
    return operator_config.wifi_networks()


def secret(name: str, default: str = "", path: Path | None = None) -> str:
    if path is not None:
        value: Any = services(path).get(name, default)
        return str(value or "").strip()
    return operator_config.secret_value(name, default)


def load_into_environ(*, override: bool = True, path: Path | None = None) -> None:
    for source, target in _ENV_KEYS.items():
        value = secret(source, path=path)
        if not value:
            continue
        if override or not os.environ.get(target):
            os.environ[target] = value
