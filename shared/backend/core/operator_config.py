"""Versioned, side-effect-free operator configuration for InkSight.

Four layers are deliberately kept separate:

* public/user configuration (``inksight_config.json``), without secret values;
* private secrets (``inksight_secrets.json``), mode 0600;
* runtime state (the existing ``backend/state`` stores, never loaded here);
* fixed hardware definitions (firmware source/build flags, never loaded here).

``manual_settings.json`` remains a read-only compatibility source until the
operator explicitly applies a migration. Normal loading never writes, starts a
scheduler, opens a socket, or imports a service adapter. The sole write-capable
read path is deterministic recovery of a durable transaction journal left by
an interrupted configuration-pair commit.
"""
from __future__ import annotations

import copy
import base64
import binascii
import hashlib
import json
import math
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterator

from . import news_model_limits

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows packaging
    fcntl = None
    import msvcrt  # type: ignore


SHARED_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = SHARED_ROOT / "config"
CONFIG_FILE = CONFIG_DIR / "inksight_config.json"
SECRETS_FILE = CONFIG_DIR / "inksight_secrets.json"
LEGACY_FILE = CONFIG_DIR / "manual_settings.json"
BACKUP_DIR = CONFIG_DIR / "backups"
SCHEMA_VERSION = 2
NEWS_SOURCE_IDS = ("ithome", "qbitai", "ars", "verge", "techcrunch", "openai")

_SECRET_ENV = {
    "deepseek_api_key": "DEEPSEEK_API_KEY",
    "news_deepseek_api_key": "NEWS_DEEPSEEK_API_KEY",
    "goldapi_key": "GOLDAPI_KEY",
}
_SECRET_REF_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,47}$")
_WIFI_FORBIDDEN = set("~^|;\"'\\$")


class ConfigError(ValueError):
    """A configuration candidate cannot safely become effective."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass
class EffectiveConfig:
    config: dict
    _secrets: dict
    sources: list[str]
    warnings: list[str]
    fingerprint: str
    degraded: bool = False
    errors: list[str] | None = None

    def clone(self) -> "EffectiveConfig":
        return EffectiveConfig(copy.deepcopy(self.config), copy.deepcopy(self._secrets),
                               list(self.sources), list(self.warnings), self.fingerprint,
                               self.degraded, list(self.errors or []))


DEFAULT_CONFIG = {
    "schema_version": SCHEMA_VERSION,
    "secret_refs": {
        "wifi_networks": "wifi.networks",
        "deepseek_api_key": "services.deepseek_api_key",
        "news_deepseek_api_key": "services.news_deepseek_api_key",
        "goldapi_key": "services.goldapi_key",
        "openai_admin_api_key": "services.openai_admin_api_key",
        "cloud_base_url": "cloud.base_url",
        "cloud_user": "cloud.user",
        "cloud_password": "cloud.password",
    },
    "codex_member": {},
    "panel_display": {
        "show_codex_credits": False,
        # Deprecated compatibility values are deliberately retained in the
        # public schema, but runtime/display code never reads them.
        "codex_credit_balance": None,
        "show_openai_api_info": False,
        "openai_api_balance_usd": None,
        "show_reset_opportunities": True,
        "show_weekday": True,
        "show_lunar": True,
        "show_solar_terms": True,
        "show_festivals": True,
    },
    "openai_costs": {
        "poll_interval_minutes": 60,
        "manual_refresh_cooldown_seconds": 60,
        "organization_id": None,
    },
    "deepseek_token_tracking": {
        "initial_date": None,
        "initial_tokens": 0,
        "initial_prompt_tokens": 0,
        "initial_completion_tokens": 0,
        "initial_complete": True,
        "balance_refresh_threshold_cny": 0.1,
        "balance_estimate_output_ratio": 0.1,
    },
    "news_digest": {
        "enabled": True,
        "model": "deepseek-v4-flash",
        "monthly_budget_cny": None,
        "max_input_tokens": 4000,
        "max_output_tokens": 500,
        "max_calls_per_issue": 5,
        "precollect_minutes": 5,
        "retry_window_minutes": 30,
        "allow_missed_catchup": True,
        "workday_cutoff": "22:00",
        "restday_cutoff": "20:00",
        "failure_policy": "revise_then_source_title_fallback",
        # New installations make no remote request until the operator enables
        # individual sources after checking the applicable terms.
        "sources": [],
        "source_enabled": {source_id: False for source_id in NEWS_SOURCE_IDS},
        "topic_preferences": [],
        "schedules": [
            {"id": "morning", "label": "科技 / AI 早报", "time": "08:55",
             "enabled": True, "day_types": ["all"]},
            {"id": "noon", "label": "科技 / AI 中报", "time": "12:55",
             "enabled": True, "day_types": ["workday"]},
            {"id": "evening", "label": "科技 / AI 晚报", "time": "16:55",
             "enabled": True, "day_types": ["workday"]},
        ],
    },
    "gold_refresh": {
        "scheduled_enabled": True,
        "mode_enabled": {"active": True, "light": True, "night": True},
        "wake_enabled": True,
        "minimum_request_interval_seconds": 30,
        "midnight_tolerance_seconds": 120,
        "intraday_recovery_cooldown_seconds": 21600,
        "intraday_recovery_max_attempts_per_day": 2,
    },
    "device_policy": {
        "timezone": "Asia/Shanghai",
        "news_check": {
            "enabled": True,
            "followup_seconds": 300,
            "followup_poll_seconds": 60,
        },
        "activity_windows": {
            "workday": [
                {"id": "work-active", "start": "08:30", "end": "18:30", "mode": "active"},
                {"id": "work-light", "start": "18:30", "end": "22:00", "mode": "light"},
                {"id": "work-night", "start": "22:00", "end": "08:30", "mode": "night"},
            ],
            "restday": [
                {"id": "rest-light", "start": "08:00", "end": "20:00", "mode": "light"},
                {"id": "rest-night", "start": "20:00", "end": "08:00", "mode": "night"},
            ],
        },
        "adaptive_check": {
            "observation_seconds": 300,
            "observation_poll_seconds": 60,
            "unknown_max_age_seconds": 1800,
            "active": [{"after_seconds": 0, "interval_seconds": 60}],
            "light": [
                {"after_seconds": 0, "interval_seconds": 60},
                {"after_seconds": 300, "interval_seconds": 180},
                {"after_seconds": 900, "interval_seconds": 300},
            ],
            "night": [
                {"after_seconds": 0, "interval_seconds": 60},
                {"after_seconds": 300, "interval_seconds": 300},
                {"after_seconds": 900, "interval_seconds": 600},
                {"after_seconds": 1800, "interval_seconds": 900},
                {"after_seconds": 3600, "interval_seconds": 1800},
            ],
        },
        "page_switch": {
            "enabled": True,
            "default_page": "ai",
            "minimum_ai_hold_seconds": 300,
            "active_idle_seconds": 600,
            "light_idle_seconds": 300,
            "night_behavior": "keep_current",
        },
        "time_sync": {
            "enabled": True,
            "interval_seconds": 3600,
            "servers": ["ntp.aliyun.com", "pool.ntp.org"],
        },
        "network": {
            "association_timeout_ms": 9000,
            "connect_round_budget_ms": 25000,
            "fetch_round_budget_ms": 60000,
            "http_timeout_ms": 15000,
            "fallback_tries": 1,
            "retry_backoff_seconds": [30, 60, 120, 300],
        },
        "calendar_overrides": {"manual_workdays": [], "manual_holidays": []},
    },
    "reserved": {
        "activity_policy": {
            "enabled": False,
            "timezone": "Asia/Shanghai",
            "windows": {
                "workday": [
                    {"id": "work-active", "start": "08:30", "end": "18:30", "mode": "active"},
                    {"id": "work-light", "start": "18:30", "end": "22:00", "mode": "light"},
                    {"id": "work-night", "start": "22:00", "end": "08:30", "mode": "night"},
                ],
                "restday": [
                    {"id": "rest-light", "start": "08:00", "end": "20:00", "mode": "light"},
                    {"id": "rest-night", "start": "20:00", "end": "08:00", "mode": "night"},
                ],
            },
            "intervals_s": {
                "active": [60], "light": [60, 180, 300],
                "night": [60, 300, 600, 900, 1800],
            },
        },
        "device_runtime": {
            "enabled": False,
            "page_switch_s": {"active": 600, "light": 300, "night": 0},
            "ntp_servers": ["ntp.aliyun.com", "pool.ntp.org"],
            "network": {
                "association_timeout_ms": 9000,
                "connect_round_budget_ms": 25000,
                "fetch_round_budget_ms": 60000,
                "http_timeout_ms": 15000,
                "fallback_tries": 1,
            },
            "calendar": {"manual_workdays": [], "manual_holidays": []},
        },
        "news": {
            "enabled": False,
            "model": "deepseek-v4-flash",
            "monthly_budget_cny": None,
            "max_input_tokens": 4000,
            "max_output_tokens": 500,
            "max_calls_per_issue": 5,
            "failure_policy": "revise_then_source_title_fallback",
            "sources": ["ithome", "qbitai", "ars", "verge", "techcrunch", "openai"],
            "schedules": [
                {"id": "weekday-morning", "label": "科技 / AI 早报", "time": "08:55",
                 "enabled": True, "day_types": ["workday"]},
                {"id": "weekday-noon", "label": "科技 / AI 中报", "time": "12:55",
                 "enabled": True, "day_types": ["workday"]},
                {"id": "weekday-evening", "label": "科技 / AI 晚报", "time": "16:55",
                 "enabled": True, "day_types": ["workday"]},
                {"id": "restday-morning", "label": "科技 / AI 早报", "time": "08:55",
                 "enabled": True, "day_types": ["restday"]},
            ],
        },
        "gold": {
            "enabled": False,
            "provider": "goldapi.io",
            "instrument": "XAU/CNY",
            "cold_start_stale_after_s": 86400,
            "shared_quota_calls_per_token": 100,
            "automatic_token_rotation": False,
            "schedules": [
                {"id": "gold-morning", "label": "上午金价", "time": "09:05",
                 "enabled": True, "day_types": ["all"]},
                {"id": "gold-afternoon", "label": "下午金价", "time": "15:05",
                 "enabled": True, "day_types": ["all"]},
            ],
        },
        "market_data": {
            "enabled": False,
            "international_gold": {"instrument": "XAU", "currency": "CNY", "unit": "g"},
            "domestic_gold": {"instrument": None, "currency": "CNY", "unit": "g"},
            "fx": {"pair": None, "display_decimals": 2, "smaller_font": True},
        },
    },
}

DEFAULT_SECRETS = {
    "schema_version": SCHEMA_VERSION,
    "wifi": {"networks": []},
    "services": {
        "deepseek_api_key": "",
        "news_deepseek_api_key": "",
        "goldapi_key": "",
        "openai_admin_api_key": "",
    },
    "cloud": {"base_url": "", "user": "", "password": ""},
}

_CONFIG_ALLOWED = {
    "schema_version": None,
    "secret_refs": {key: None for key in DEFAULT_CONFIG["secret_refs"]},
    "codex_member": {"plan": None, "renewal_status": None, "valid_until_date": None,
                     "precision": None, "source": None},
    "panel_display": {"show_codex_credits": None, "codex_credit_balance": None,
                      "show_openai_api_info": None, "openai_api_balance_usd": None,
                      "show_reset_opportunities": None,
                      "show_weekday": None, "show_lunar": None,
                      "show_solar_terms": None, "show_festivals": None},
    "openai_costs": {"poll_interval_minutes": None,
                      "manual_refresh_cooldown_seconds": None,
                      "organization_id": None},
    "deepseek_token_tracking": {key: None for key in DEFAULT_CONFIG["deepseek_token_tracking"]},
    "news_digest": {"enabled": None, "model": None, "monthly_budget_cny": None,
                    "max_input_tokens": None, "max_output_tokens": None,
                    "max_calls_per_issue": None,
                    "precollect_minutes": None, "retry_window_minutes": None,
                    "allow_missed_catchup": None,
                    "workday_cutoff": None, "restday_cutoff": None,
                    "failure_policy": None, "sources": None,
                    "source_enabled": {source_id: None for source_id in NEWS_SOURCE_IDS},
                    "topic_preferences": None, "schedules": None},
    "gold_refresh": {"scheduled_enabled": None,
                     "mode_enabled": {"active": None, "light": None, "night": None},
                     "wake_enabled": None, "minimum_request_interval_seconds": None,
                     "midnight_tolerance_seconds": None,
                     "intraday_recovery_cooldown_seconds": None,
                     "intraday_recovery_max_attempts_per_day": None},
    "device_policy": {
        "timezone": None,
        "news_check": {"enabled": None, "followup_seconds": None,
                       "followup_poll_seconds": None},
        "activity_windows": {"workday": None, "restday": None},
        "adaptive_check": {
            "observation_seconds": None, "observation_poll_seconds": None,
            "unknown_max_age_seconds": None,
            "active": None, "light": None, "night": None,
        },
        "page_switch": {"enabled": None, "default_page": None,
                        "minimum_ai_hold_seconds": None, "active_idle_seconds": None,
                        "light_idle_seconds": None, "night_behavior": None},
        "time_sync": {"enabled": None, "interval_seconds": None, "servers": None},
        "network": {"association_timeout_ms": None, "connect_round_budget_ms": None,
                    "fetch_round_budget_ms": None, "http_timeout_ms": None,
                    "fallback_tries": None, "retry_backoff_seconds": None},
        "calendar_overrides": {"manual_workdays": None, "manual_holidays": None},
    },
    "reserved": {
        "activity_policy": {"enabled": None, "timezone": None,
                            "windows": {"workday": None, "restday": None},
                            "intervals_s": {"active": None, "light": None, "night": None}},
        "device_runtime": {"enabled": None,
                           "page_switch_s": {"active": None, "light": None, "night": None},
                           "ntp_servers": None,
                           "network": {"association_timeout_ms": None,
                                       "connect_round_budget_ms": None,
                                       "fetch_round_budget_ms": None,
                                       "http_timeout_ms": None, "fallback_tries": None},
                           "calendar": {"manual_workdays": None, "manual_holidays": None}},
        "news": {"enabled": None, "model": None, "monthly_budget_cny": None,
                 "max_input_tokens": None, "max_output_tokens": None,
                 "max_calls_per_issue": None,
                 "failure_policy": None, "sources": None, "schedules": None},
        "gold": {"enabled": None, "provider": None, "instrument": None,
                 "cold_start_stale_after_s": None, "shared_quota_calls_per_token": None,
                 "automatic_token_rotation": None, "schedules": None},
        "market_data": {"enabled": None,
                        "international_gold": {"instrument": None, "currency": None, "unit": None},
                        "domestic_gold": {"instrument": None, "currency": None, "unit": None},
                        "fx": {"pair": None, "display_decimals": None, "smaller_font": None}},
    },
}

_SECRETS_ALLOWED = {
    "schema_version": None,
    "wifi": {"networks": None},
    "services": {"deepseek_api_key": None, "news_deepseek_api_key": None,
                 "goldapi_key": None, "openai_admin_api_key": None},
    "cloud": {"base_url": None, "user": None, "password": None},
}

_CACHE_LOCK = threading.RLock()
_LAST_KNOWN_GOOD: dict[tuple[str, str, str], EffectiveConfig] = {}
_FILE_LOCKS: dict[str, threading.RLock] = {}


def _read_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except OSError as exc:
        return None, f"io:{type(exc).__name__}"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON at line {exc.lineno} column {exc.colno}"
    return (value, None) if isinstance(value, dict) else (None, "root must be an object")


def _decode_json(raw: bytes | None, name: str) -> tuple[dict | None, str | None]:
    if raw is None:
        return None, "missing"
    try:
        value = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return None, "invalid UTF-8"
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON at line {exc.lineno} column {exc.colno}"
    return (value, None) if isinstance(value, dict) else (None, "root must be an object")


def _file_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _pair_names(config_path: Path, secrets_path: Path) -> tuple[Path, Path]:
    identity = f"{Path(config_path).resolve()}\0{Path(secrets_path).resolve()}".encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:12]
    parent = Path(config_path).parent
    return (parent / f"inksight-config-pair-{suffix}",
            parent / f".inksight-config-pair-{suffix}.txn.json")


def _pair_thread_lock(config_path: Path, secrets_path: Path) -> threading.RLock:
    anchor, _ = _pair_names(config_path, secrets_path)
    key = f"pair:{anchor.resolve()}"
    with _CACHE_LOCK:
        return _FILE_LOCKS.setdefault(key, threading.RLock())


def _stable_pair_snapshot(config_path: Path, secrets_path: Path,
                          legacy_path: Path) -> tuple[bytes | None, bytes | None, bytes | None]:
    """Read a stable effective-input snapshot without creating lock files.

    A writer publishes a durable journal before touching either target. Two
    identical passes plus journal checks prevent a reader from accepting a
    pair that changed entirely between its individual file reads.
    """
    config_path, secrets_path, legacy_path = map(Path, (config_path, secrets_path, legacy_path))
    anchor, journal = _pair_names(config_path, secrets_path)
    thread_lock = _pair_thread_lock(config_path, secrets_path)
    with thread_lock:
        for _ in range(8):
            if journal.exists():
                with _file_lock(anchor):
                    _recover_pair_locked(config_path, secrets_path, journal)
            first = (_file_bytes(config_path), _file_bytes(secrets_path), _file_bytes(legacy_path))
            if journal.exists():
                continue
            second = (_file_bytes(config_path), _file_bytes(secrets_path), _file_bytes(legacy_path))
            if first == second and not journal.exists():
                return second
        # Persistent journal or very high churn: serialize once with the writer
        # and either recover or take a final stable snapshot.
        with _file_lock(anchor):
            _recover_pair_locked(config_path, secrets_path, journal)
            return (_file_bytes(config_path), _file_bytes(secrets_path), _file_bytes(legacy_path))


def _merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _reject_unknown(value: Any, allowed: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, dict) or not isinstance(allowed, dict):
        return
    for key, child in value.items():
        child_path = f"{path}.{key}" if path else key
        if key not in allowed:
            errors.append(f"{child_path}: unknown field")
        elif isinstance(allowed[key], dict):
            if not isinstance(child, dict):
                errors.append(f"{child_path}: expected object")
            else:
                _reject_unknown(child, allowed[key], child_path, errors)


def _finite_number(value: Any, path: str, errors: list[str], *, minimum: float | None = None,
                   maximum: float | None = None, integer: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{path}: expected {'integer' if integer else 'number'}")
        return
    try:
        finite = math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        finite = False
    if not finite or (integer and not isinstance(value, int)):
        errors.append(f"{path}: expected finite {'integer' if integer else 'number'}")
        return
    if minimum is not None and value < minimum:
        errors.append(f"{path}: must be >= {minimum:g}")
    if maximum is not None and value > maximum:
        errors.append(f"{path}: must be <= {maximum:g}")


def _parse_date(value: Any, path: str, errors: list[str], *, nullable: bool = True) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value:
        errors.append(f"{path}: expected YYYY-MM-DD{' or null' if nullable else ''}")
        return
    try:
        date.fromisoformat(value)
    except ValueError:
        errors.append(f"{path}: invalid calendar date, expected YYYY-MM-DD")


def _nonempty_string(value: Any, path: str, errors: list[str], *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}: expected non-empty string{' or null' if nullable else ''}")


def _minute(value: Any, path: str, errors: list[str]) -> int | None:
    if not isinstance(value, str) or not _TIME_RE.match(value):
        errors.append(f"{path}: expected HH:MM (00:00-23:59)")
        return None
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _validate_windows(rows: Any, path: str, errors: list[str]) -> None:
    if not isinstance(rows, list) or not rows:
        errors.append(f"{path}: expected non-empty list")
        return
    ids: set[str] = set()
    spans: list[tuple[int, int, str]] = []
    for index, row in enumerate(rows):
        item_path = f"{path}[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{item_path}: expected object")
            continue
        _reject_unknown(row, {"id": None, "start": None, "end": None, "mode": None}, item_path, errors)
        ident = row.get("id")
        if not isinstance(ident, str) or not _ID_RE.match(ident):
            errors.append(f"{item_path}.id: expected stable lowercase id")
        elif ident in ids:
            errors.append(f"{item_path}.id: duplicate id {ident}")
        else:
            ids.add(ident)
        start = _minute(row.get("start"), f"{item_path}.start", errors)
        end = _minute(row.get("end"), f"{item_path}.end", errors)
        if row.get("mode") not in ("active", "light", "night"):
            errors.append(f"{item_path}.mode: expected active, light, or night")
        if start is not None and end is not None:
            if start == end:
                errors.append(f"{item_path}: start and end cannot be equal")
            else:
                spans.append((start, end if end > start else end + 1440, str(ident)))
    for i, (a0, a1, aid) in enumerate(spans):
        for b0, b1, bid in spans[i + 1:]:
            overlap = any(max(a0, x0) < min(a1, x1)
                          for x0, x1 in ((b0, b1), (b0 - 1440, b1 - 1440),
                                         (b0 + 1440, b1 + 1440)))
            if overlap:
                errors.append(f"{path}: windows {aid} and {bid} overlap")


def _validate_schedule(rows: Any, path: str, errors: list[str], day_types: set[str]) -> None:
    if not isinstance(rows, list):
        errors.append(f"{path}: expected list")
        return
    ids: set[str] = set()
    occupied: set[tuple[str, str]] = set()
    minutes_by_day: dict[str, list[tuple[int, str]]] = {
        day: [] for day in ("workday", "restday") if day in day_types or "all" in day_types}
    for index, row in enumerate(rows):
        item_path = f"{path}[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{item_path}: expected object")
            continue
        _reject_unknown(row, {"id": None, "label": None, "time": None,
                              "cutoff": None, "enabled": None, "day_types": None},
                        item_path, errors)
        ident = row.get("id")
        if not isinstance(ident, str) or not _ID_RE.match(ident):
            errors.append(f"{item_path}.id: expected stable lowercase id")
        elif ident in ids:
            errors.append(f"{item_path}.id: duplicate id {ident}")
        else:
            ids.add(ident)
        if not isinstance(row.get("label"), str) or not row.get("label"):
            errors.append(f"{item_path}.label: expected non-empty string")
        elif len(row["label"].encode("utf-8")) > 48:
            errors.append(f"{item_path}.label: exceeds panel title budget (48 UTF-8 bytes)")
        time_value = row.get("time")
        minute_value = _minute(time_value, f"{item_path}.time", errors)
        cutoff_value = row.get("cutoff")
        cutoff_minute = None
        if cutoff_value is not None:
            cutoff_minute = _minute(cutoff_value, f"{item_path}.cutoff", errors)
            if (minute_value is not None and cutoff_minute is not None
                    and cutoff_minute <= minute_value):
                errors.append(f"{item_path}.cutoff: must be later than time on the same day")
        if not isinstance(row.get("enabled"), bool):
            errors.append(f"{item_path}.enabled: expected true or false")
        dts = row.get("day_types")
        if not isinstance(dts, list) or not dts:
            errors.append(f"{item_path}.day_types: expected non-empty list")
            continue
        if len(set(str(v) for v in dts)) != len(dts):
            errors.append(f"{item_path}.day_types: duplicate day type")
        expanded: set[str] = set()
        for day_type in dts:
            if day_type not in day_types:
                errors.append(f"{item_path}.day_types: unsupported value {day_type!r}")
            elif day_type == "all":
                expanded.update(("workday", "restday"))
            else:
                expanded.add(day_type)
        if row.get("enabled") is True and isinstance(time_value, str):
            for actual_day in expanded:
                slot = (actual_day, time_value)
                if slot in occupied:
                    errors.append(f"{item_path}: duplicate enabled slot {actual_day} {time_value}")
                occupied.add(slot)
                if minute_value is not None and actual_day in minutes_by_day:
                    minutes_by_day[actual_day].append((minute_value, str(ident)))
    for actual_day, values in minutes_by_day.items():
        if len(values) > 12:
            errors.append(f"{path}: at most 12 enabled schedules per {actual_day}")
        ordered = sorted(values)
        for index, (minute, ident) in enumerate(ordered):
            next_minute, next_ident = ordered[(index + 1) % len(ordered)] if ordered else (minute, ident)
            gap = (next_minute - minute) % 1440
            if len(ordered) > 1 and gap < 30:
                errors.append(f"{path}: enabled schedules {ident} and {next_ident} are less than 30 minutes apart on {actual_day}")


def _validate_adaptive_steps(rows: Any, path: str, errors: list[str]) -> None:
    if not isinstance(rows, list) or not rows:
        errors.append(f"{path}: expected non-empty list")
        return
    previous = -1
    for index, row in enumerate(rows):
        item_path = f"{path}[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{item_path}: expected object")
            continue
        _reject_unknown(row, {"after_seconds": None, "interval_seconds": None}, item_path, errors)
        after = row.get("after_seconds")
        interval = row.get("interval_seconds")
        _finite_number(after, f"{item_path}.after_seconds", errors, minimum=0,
                       maximum=86400, integer=True)
        _finite_number(interval, f"{item_path}.interval_seconds", errors, minimum=10,
                       maximum=86400, integer=True)
        if isinstance(after, int):
            if index == 0 and after != 0:
                errors.append(f"{path}: first after_seconds must be 0")
            if after <= previous:
                errors.append(f"{path}: after_seconds must be strictly increasing")
            previous = after


def validate_config(config: dict) -> list[str]:
    errors: list[str] = []
    _reject_unknown(config, _CONFIG_ALLOWED, "", errors)
    if config.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version: expected {SCHEMA_VERSION}; future/unknown versions are not activated")
    refs = config.get("secret_refs")
    if not isinstance(refs, dict):
        errors.append("secret_refs: expected object")
    else:
        for key, value in refs.items():
            if not isinstance(value, str) or not _SECRET_REF_RE.match(value):
                errors.append(f"secret_refs.{key}: expected dotted secret_ref such as services.api_key")

    member = config.get("codex_member")
    if not isinstance(member, dict):
        errors.append("codex_member: expected object")
    elif member:
        if member.get("plan") not in ("FREE", "PLUS", "PRO", "BUSINESS", "ENTERPRISE"):
            errors.append("codex_member.plan: expected FREE, PLUS, PRO, BUSINESS, or ENTERPRISE")
        if member.get("renewal_status") not in ("active", "cancelled", "expired", "unknown"):
            errors.append("codex_member.renewal_status: expected active, cancelled, expired, or unknown")
        _parse_date(member.get("valid_until_date"), "codex_member.valid_until_date", errors)
        if member.get("precision") != "date":
            errors.append("codex_member.precision: only date is supported; do not invent a time")
        if member.get("source") != "manual":
            errors.append("codex_member.source: expected manual")

    panel = config.get("panel_display")
    if not isinstance(panel, dict):
        errors.append("panel_display: expected object")
    else:
        for key in ("show_codex_credits", "show_openai_api_info", "show_reset_opportunities",
                    "show_weekday", "show_lunar", "show_solar_terms", "show_festivals"):
            if not isinstance(panel.get(key), bool):
                errors.append(f"panel_display.{key}: expected true or false")
        balance = panel.get("openai_api_balance_usd")
        if balance is not None:
            _finite_number(balance, "panel_display.openai_api_balance_usd", errors, minimum=0)
        credits_balance = panel.get("codex_credit_balance")
        if credits_balance is not None:
            _finite_number(credits_balance, "panel_display.codex_credit_balance", errors,
                           minimum=0)

    costs = config.get("openai_costs")
    if not isinstance(costs, dict):
        errors.append("openai_costs: expected object")
    else:
        _finite_number(costs.get("poll_interval_minutes"),
                       "openai_costs.poll_interval_minutes", errors,
                       minimum=5, maximum=1440, integer=True)
        _finite_number(costs.get("manual_refresh_cooldown_seconds"),
                       "openai_costs.manual_refresh_cooldown_seconds", errors,
                       minimum=10, maximum=3600, integer=True)
        organization = costs.get("organization_id")
        if organization is not None and (not isinstance(organization, str)
                                         or not organization.strip()
                                         or len(organization) > 128):
            errors.append("openai_costs.organization_id: expected a non-empty string or null")

    tracking = config.get("deepseek_token_tracking")
    if not isinstance(tracking, dict):
        errors.append("deepseek_token_tracking: expected object")
    else:
        _parse_date(tracking.get("initial_date"), "deepseek_token_tracking.initial_date", errors)
        for key in ("initial_tokens", "initial_prompt_tokens", "initial_completion_tokens"):
            _finite_number(tracking.get(key), f"deepseek_token_tracking.{key}", errors,
                           minimum=0, integer=True)
        if not isinstance(tracking.get("initial_complete"), bool):
            errors.append("deepseek_token_tracking.initial_complete: expected true or false")
        _finite_number(tracking.get("balance_refresh_threshold_cny"),
                       "deepseek_token_tracking.balance_refresh_threshold_cny", errors,
                       minimum=0.01)
        _finite_number(tracking.get("balance_estimate_output_ratio"),
                       "deepseek_token_tracking.balance_estimate_output_ratio", errors,
                       minimum=0, maximum=1)

    news_active = config.get("news_digest")
    if not isinstance(news_active, dict):
        errors.append("news_digest: expected object")
    else:
        if not isinstance(news_active.get("enabled"), bool):
            errors.append("news_digest.enabled: expected true or false")
        _nonempty_string(news_active.get("model"), "news_digest.model", errors)
        if news_active.get("model") != "deepseek-v4-flash":
            errors.append("news_digest.model: only deepseek-v4-flash is supported")
        if news_active.get("monthly_budget_cny") is not None:
            _finite_number(news_active.get("monthly_budget_cny"),
                           "news_digest.monthly_budget_cny", errors, minimum=0.01)
        for problem in news_model_limits.validate_generation_limits(
                str(news_active.get("model") or ""), news_active.get("max_input_tokens"),
                news_active.get("max_output_tokens")):
            errors.append("news_digest." + problem)
        _finite_number(news_active.get("max_calls_per_issue"),
                       "news_digest.max_calls_per_issue", errors, minimum=1, integer=True)
        _finite_number(news_active.get("precollect_minutes"),
                       "news_digest.precollect_minutes", errors, minimum=0, maximum=30,
                       integer=True)
        _finite_number(news_active.get("retry_window_minutes"),
                       "news_digest.retry_window_minutes", errors, minimum=1, maximum=120,
                       integer=True)
        if not isinstance(news_active.get("allow_missed_catchup"), bool):
            errors.append("news_digest.allow_missed_catchup: expected true or false")
        for key in ("workday_cutoff", "restday_cutoff"):
            _minute(news_active.get(key), f"news_digest.{key}", errors)
        if news_active.get("failure_policy") != "revise_then_source_title_fallback":
            errors.append("news_digest.failure_policy: unsupported policy")
        for field in ("sources", "topic_preferences"):
            values = news_active.get(field)
            if not isinstance(values, list):
                errors.append(f"news_digest.{field}: expected list")
            elif any(not isinstance(value, str) or not value.strip() for value in values):
                errors.append(f"news_digest.{field}: values must be non-empty strings")
            elif len(values) != len(set(values)):
                errors.append(f"news_digest.{field}: duplicate value")
        known_sources = {"ithome", "qbitai", "ars", "verge", "techcrunch", "openai"}
        if isinstance(news_active.get("sources"), list):
            unknown_sources = sorted(set(news_active["sources"]) - known_sources)
            if unknown_sources:
                errors.append("news_digest.sources: unsupported source " + ", ".join(unknown_sources))
        source_enabled = news_active.get("source_enabled")
        if not isinstance(source_enabled, dict):
            errors.append("news_digest.source_enabled: expected object")
        else:
            for source_id in NEWS_SOURCE_IDS:
                if not isinstance(source_enabled.get(source_id), bool):
                    errors.append(
                        f"news_digest.source_enabled.{source_id}: expected true or false")
        if isinstance(news_active.get("topic_preferences"), list) and len(news_active["topic_preferences"]) > 20:
            errors.append("news_digest.topic_preferences: at most 20 entries")
        _validate_schedule(news_active.get("schedules"), "news_digest.schedules", errors,
                           {"all", "workday", "restday"})
        cutoff_minutes: dict[str, int | None] = {}
        for day_type, key in (("workday", "workday_cutoff"),
                              ("restday", "restday_cutoff")):
            value = news_active.get(key)
            if isinstance(value, str) and _TIME_RE.match(value):
                hour, minute = value.split(":")
                cutoff_minutes[day_type] = int(hour) * 60 + int(minute)
            else:
                cutoff_minutes[day_type] = None
        if isinstance(news_active.get("schedules"), list):
            for index, row in enumerate(news_active["schedules"]):
                if not isinstance(row, dict) or row.get("enabled") is not True:
                    continue
                value = row.get("time")
                if not isinstance(value, str) or not _TIME_RE.match(value):
                    continue
                hour, minute = value.split(":")
                start_minute = int(hour) * 60 + int(minute)
                declared = row.get("day_types") if isinstance(row.get("day_types"), list) else []
                actual_days = {"workday", "restday"} if "all" in declared else set(declared)
                for day_type in actual_days & {"workday", "restday"}:
                    cutoff_minute = cutoff_minutes.get(day_type)
                    if cutoff_minute is not None and start_minute >= cutoff_minute:
                        errors.append(
                            f"news_digest.schedules[{index}].time: must be earlier than "
                            f"{day_type} daily cutoff; cross-midnight catchup is unsupported")

    gold_active = config.get("gold_refresh")
    if not isinstance(gold_active, dict):
        errors.append("gold_refresh: expected object")
    else:
        for field in ("scheduled_enabled", "wake_enabled"):
            if not isinstance(gold_active.get(field), bool):
                errors.append(f"gold_refresh.{field}: expected true or false")
        modes = gold_active.get("mode_enabled")
        if not isinstance(modes, dict):
            errors.append("gold_refresh.mode_enabled: expected object")
        else:
            for mode in ("active", "light", "night"):
                if not isinstance(modes.get(mode), bool):
                    errors.append(f"gold_refresh.mode_enabled.{mode}: expected true or false")
        _finite_number(gold_active.get("minimum_request_interval_seconds"),
                       "gold_refresh.minimum_request_interval_seconds", errors,
                       minimum=30, maximum=3600, integer=True)
        _finite_number(gold_active.get("midnight_tolerance_seconds"),
                       "gold_refresh.midnight_tolerance_seconds", errors,
                       minimum=0, maximum=600, integer=True)
        _finite_number(gold_active.get("intraday_recovery_cooldown_seconds"),
                       "gold_refresh.intraday_recovery_cooldown_seconds", errors,
                       minimum=1800, maximum=86400, integer=True)
        _finite_number(gold_active.get("intraday_recovery_max_attempts_per_day"),
                       "gold_refresh.intraday_recovery_max_attempts_per_day", errors,
                       minimum=1, maximum=8, integer=True)

    policy = config.get("device_policy")
    if not isinstance(policy, dict):
        errors.append("device_policy: expected object")
    else:
        if policy.get("timezone") != "Asia/Shanghai":
            errors.append("device_policy.timezone: only Asia/Shanghai is supported")
        news_check = policy.get("news_check") or {}
        if not isinstance(news_check.get("enabled"), bool):
            errors.append("device_policy.news_check.enabled: expected true or false")
        _finite_number(news_check.get("followup_seconds"),
                       "device_policy.news_check.followup_seconds", errors,
                       minimum=0, maximum=900, integer=True)
        _finite_number(news_check.get("followup_poll_seconds"),
                       "device_policy.news_check.followup_poll_seconds", errors,
                       minimum=30, maximum=300, integer=True)
        aw = policy.get("activity_windows") or {}
        _validate_windows(aw.get("workday"), "device_policy.activity_windows.workday", errors)
        _validate_windows(aw.get("restday"), "device_policy.activity_windows.restday", errors)
        adaptive = policy.get("adaptive_check") or {}
        _finite_number(adaptive.get("observation_seconds"),
                       "device_policy.adaptive_check.observation_seconds", errors,
                       minimum=0, maximum=3600, integer=True)
        _finite_number(adaptive.get("observation_poll_seconds"),
                       "device_policy.adaptive_check.observation_poll_seconds", errors,
                       minimum=10, maximum=3600, integer=True)
        _finite_number(adaptive.get("unknown_max_age_seconds"),
                       "device_policy.adaptive_check.unknown_max_age_seconds", errors,
                       minimum=60, maximum=86400, integer=True)
        for mode in ("active", "light", "night"):
            _validate_adaptive_steps(adaptive.get(mode),
                                     f"device_policy.adaptive_check.{mode}", errors)
        switch = policy.get("page_switch") or {}
        if not isinstance(switch.get("enabled"), bool):
            errors.append("device_policy.page_switch.enabled: expected true or false")
        if switch.get("default_page") not in ("ai", "news_gold"):
            errors.append("device_policy.page_switch.default_page: expected ai or news_gold")
        if switch.get("night_behavior") not in ("keep_current", "force_ai"):
            errors.append("device_policy.page_switch.night_behavior: expected keep_current or force_ai")
        for key in ("minimum_ai_hold_seconds", "active_idle_seconds", "light_idle_seconds"):
            _finite_number(switch.get(key), f"device_policy.page_switch.{key}", errors,
                           minimum=0, maximum=86400, integer=True)
        ntp = policy.get("time_sync") or {}
        if not isinstance(ntp.get("enabled"), bool):
            errors.append("device_policy.time_sync.enabled: expected true or false")
        _finite_number(ntp.get("interval_seconds"), "device_policy.time_sync.interval_seconds",
                       errors, minimum=300, maximum=7 * 86400, integer=True)
        servers = ntp.get("servers")
        if not isinstance(servers, list) or not 1 <= len(servers) <= 3:
            errors.append("device_policy.time_sync.servers: expected 1-3 entries")
        elif any(not isinstance(value, str) or not value.strip() for value in servers):
            errors.append("device_policy.time_sync.servers: values must be non-empty strings")
        elif len(servers) != len(set(servers)):
            errors.append("device_policy.time_sync.servers: duplicate server")
        net = policy.get("network") or {}
        for key in ("association_timeout_ms", "connect_round_budget_ms", "fetch_round_budget_ms",
                    "http_timeout_ms", "fallback_tries"):
            _finite_number(net.get(key), f"device_policy.network.{key}", errors,
                           minimum=0 if key == "fallback_tries" else 1,
                           maximum=300000, integer=True)
        backoff = net.get("retry_backoff_seconds")
        if not isinstance(backoff, list) or not backoff:
            errors.append("device_policy.network.retry_backoff_seconds: expected non-empty list")
        elif (any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in backoff)
              or backoff != sorted(set(backoff))):
            errors.append("device_policy.network.retry_backoff_seconds: values must be positive, unique and increasing")
        if all(isinstance(net.get(key), int) for key in
               ("association_timeout_ms", "connect_round_budget_ms", "fetch_round_budget_ms",
                "http_timeout_ms")):
            if net["association_timeout_ms"] > net["connect_round_budget_ms"]:
                errors.append("device_policy.network: association timeout exceeds connect round budget")
            if net["connect_round_budget_ms"] + net["http_timeout_ms"] > net["fetch_round_budget_ms"]:
                errors.append("device_policy.network: connect + HTTP timeout exceeds fetch round budget")
        overrides = policy.get("calendar_overrides") or {}
        for group in ("manual_workdays", "manual_holidays"):
            values = overrides.get(group)
            if not isinstance(values, list):
                errors.append(f"device_policy.calendar_overrides.{group}: expected list")
            else:
                for index, value in enumerate(values):
                    _parse_date(value, f"device_policy.calendar_overrides.{group}[{index}]",
                                errors, nullable=False)
                if len(values) != len(set(values)):
                    errors.append(f"device_policy.calendar_overrides.{group}: duplicate date")
        if isinstance(overrides.get("manual_workdays"), list) and isinstance(overrides.get("manual_holidays"), list):
            if set(overrides["manual_workdays"]) & set(overrides["manual_holidays"]):
                errors.append("device_policy.calendar_overrides: a date cannot be both workday and holiday")

    reserved = config.get("reserved")
    if not isinstance(reserved, dict):
        errors.append("reserved: expected object")
        return errors
    for name, section in reserved.items():
        if not isinstance(section, dict):
            continue
        if section.get("enabled") is not False:
            errors.append(f"reserved.{name}.enabled: must remain false in schema v1; implementation is pending")

    activity = reserved.get("activity_policy") or {}
    if activity.get("timezone") != "Asia/Shanghai":
        errors.append("reserved.activity_policy.timezone: schema v1 supports Asia/Shanghai only")
    windows = activity.get("windows") or {}
    _validate_windows(windows.get("workday"), "reserved.activity_policy.windows.workday", errors)
    _validate_windows(windows.get("restday"), "reserved.activity_policy.windows.restday", errors)
    intervals = activity.get("intervals_s") or {}
    for mode in ("active", "light", "night"):
        values = intervals.get(mode)
        if not isinstance(values, list) or not values:
            errors.append(f"reserved.activity_policy.intervals_s.{mode}: expected non-empty list")
        elif any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in values):
            errors.append(f"reserved.activity_policy.intervals_s.{mode}: values must be positive integers")
        elif values != sorted(set(values)):
            errors.append(f"reserved.activity_policy.intervals_s.{mode}: values must be unique and increasing")

    device = reserved.get("device_runtime") or {}
    page_switch = device.get("page_switch_s") or {}
    for mode in ("active", "light", "night"):
        _finite_number(page_switch.get(mode), f"reserved.device_runtime.page_switch_s.{mode}",
                       errors, minimum=0, integer=True)
    ntp_servers = device.get("ntp_servers")
    if not isinstance(ntp_servers, list) or not ntp_servers:
        errors.append("reserved.device_runtime.ntp_servers: expected non-empty list")
    elif any(not isinstance(value, str) or not value.strip() for value in ntp_servers):
        errors.append("reserved.device_runtime.ntp_servers: values must be non-empty strings")
    elif len(ntp_servers) != len(set(ntp_servers)):
        errors.append("reserved.device_runtime.ntp_servers: duplicate server")
    for key, value in (device.get("network") or {}).items():
        _finite_number(value, f"reserved.device_runtime.network.{key}", errors,
                       minimum=0 if key == "fallback_tries" else 1, integer=True)
    network = device.get("network") or {}
    if all(isinstance(network.get(key), int) for key in
           ("association_timeout_ms", "connect_round_budget_ms", "fetch_round_budget_ms", "http_timeout_ms")):
        if network["association_timeout_ms"] > network["connect_round_budget_ms"]:
            errors.append("reserved.device_runtime.network: association timeout exceeds connect round budget")
        if network["connect_round_budget_ms"] + network["http_timeout_ms"] > network["fetch_round_budget_ms"]:
            errors.append("reserved.device_runtime.network: connect + HTTP timeout exceeds fetch round budget")
    for group in ("manual_workdays", "manual_holidays"):
        values = (device.get("calendar") or {}).get(group)
        if not isinstance(values, list):
            errors.append(f"reserved.device_runtime.calendar.{group}: expected list")
        else:
            for index, value in enumerate(values):
                _parse_date(value, f"reserved.device_runtime.calendar.{group}[{index}]", errors,
                            nullable=False)
    calendar = device.get("calendar") or {}
    workdays = calendar.get("manual_workdays")
    holidays = calendar.get("manual_holidays")
    if isinstance(workdays, list) and len(workdays) != len(set(workdays)):
        errors.append("reserved.device_runtime.calendar.manual_workdays: duplicate date")
    if isinstance(holidays, list) and len(holidays) != len(set(holidays)):
        errors.append("reserved.device_runtime.calendar.manual_holidays: duplicate date")
    if isinstance(workdays, list) and isinstance(holidays, list) and set(workdays) & set(holidays):
        errors.append("reserved.device_runtime.calendar: a date cannot be both workday and holiday")

    news = reserved.get("news") or {}
    _nonempty_string(news.get("model"), "reserved.news.model", errors)
    if news.get("model") != "deepseek-v4-flash":
        errors.append("reserved.news.model: only deepseek-v4-flash is supported")
    if news.get("monthly_budget_cny") is not None:
        _finite_number(news.get("monthly_budget_cny"), "reserved.news.monthly_budget_cny", errors, minimum=0.01)
    for problem in news_model_limits.validate_generation_limits(
            str(news.get("model") or ""), news.get("max_input_tokens"),
            news.get("max_output_tokens")):
        errors.append("reserved.news." + problem)
    _finite_number(news.get("max_calls_per_issue"), "reserved.news.max_calls_per_issue", errors,
                   minimum=1, integer=True)
    if news.get("failure_policy") != "revise_then_source_title_fallback":
        errors.append("reserved.news.failure_policy: unsupported policy")
    if not isinstance(news.get("sources"), list) or not news.get("sources"):
        errors.append("reserved.news.sources: expected non-empty list")
    elif any(not isinstance(value, str) or not value.strip() for value in news["sources"]):
        errors.append("reserved.news.sources: values must be non-empty strings")
    elif len(news["sources"]) != len(set(news["sources"])):
        errors.append("reserved.news.sources: duplicate source")
    _validate_schedule(news.get("schedules"), "reserved.news.schedules", errors,
                       {"workday", "restday"})

    gold = reserved.get("gold") or {}
    _nonempty_string(gold.get("provider"), "reserved.gold.provider", errors)
    _nonempty_string(gold.get("instrument"), "reserved.gold.instrument", errors)
    _finite_number(gold.get("cold_start_stale_after_s"), "reserved.gold.cold_start_stale_after_s",
                   errors, minimum=1, integer=True)
    _finite_number(gold.get("shared_quota_calls_per_token"),
                   "reserved.gold.shared_quota_calls_per_token", errors, minimum=1, integer=True)
    if not isinstance(gold.get("automatic_token_rotation"), bool):
        errors.append("reserved.gold.automatic_token_rotation: expected true or false")
    _validate_schedule(gold.get("schedules"), "reserved.gold.schedules", errors, {"all"})

    market = reserved.get("market_data") or {}
    for section_name in ("international_gold", "domestic_gold"):
        section = market.get(section_name) or {}
        _nonempty_string(section.get("instrument"),
                         f"reserved.market_data.{section_name}.instrument", errors,
                         nullable=section_name == "domestic_gold")
        _nonempty_string(section.get("currency"),
                         f"reserved.market_data.{section_name}.currency", errors)
        _nonempty_string(section.get("unit"), f"reserved.market_data.{section_name}.unit", errors)
    fx = market.get("fx") or {}
    _nonempty_string(fx.get("pair"), "reserved.market_data.fx.pair", errors, nullable=True)
    _finite_number(fx.get("display_decimals"), "reserved.market_data.fx.display_decimals",
                   errors, minimum=0, maximum=6, integer=True)
    if not isinstance(fx.get("smaller_font"), bool):
        errors.append("reserved.market_data.fx.smaller_font: expected true or false")
    return errors


def _upgrade_public_document(config: dict) -> dict:
    """Upgrade the side-effect-free v1 document in memory.

    v1 reserved device fields were explicitly inactive, so they are retained
    for audit but are not silently copied into the active v2 device policy.
    """
    out = copy.deepcopy(config)
    if out.get("schema_version") == 1:
        out["schema_version"] = SCHEMA_VERSION
    news = out.get("news_digest")
    if isinstance(news, dict) and "source_enabled" not in news and isinstance(news.get("sources"), list):
        # Existing installations used ``sources`` as an explicit allow-list.
        # Convert it in memory so an upgrade never silently changes the user's
        # choices; the file is only rewritten after an intentional save.
        selected = set(news["sources"])
        news["source_enabled"] = {
            source_id: source_id in selected for source_id in NEWS_SOURCE_IDS
        }
    return out


def _upgrade_secrets_document(secrets: dict) -> dict:
    out = copy.deepcopy(secrets)
    if out.get("schema_version") == 1:
        out["schema_version"] = SCHEMA_VERSION
    return out


def _validate_public_document(config: dict) -> list[str]:
    """Validate a versioned public document, allowing ordinary partial overlays."""
    errors: list[str] = []
    upgraded = _upgrade_public_document(config)
    _reject_unknown(upgraded, _CONFIG_ALLOWED, "", errors)
    if upgraded.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"schema_version: expected {SCHEMA_VERSION}; future/unknown versions are not activated")
    if not errors:
        errors.extend(validate_config(_merge(DEFAULT_CONFIG, upgraded)))
    return errors


def validate_secrets(secrets: dict, *, path: Path | None = None,
                     enforce_permissions: bool = True) -> list[str]:
    errors: list[str] = []
    _reject_unknown(secrets, _SECRETS_ALLOWED, "", errors)
    if secrets.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"secrets.schema_version: expected {SCHEMA_VERSION}")
    networks = (secrets.get("wifi") or {}).get("networks")
    if not isinstance(networks, list):
        errors.append("wifi.networks: expected list")
    else:
        if len(networks) > 5:
            errors.append("wifi.networks: at most 5 entries")
        active: list[str] = []
        for index, row in enumerate(networks):
            item_path = f"wifi.networks[{index}]"
            if not isinstance(row, dict):
                errors.append(f"{item_path}: expected object")
                continue
            _reject_unknown(row, {"ssid": None, "password": None}, item_path, errors)
            ssid, password = row.get("ssid"), row.get("password")
            if not isinstance(ssid, str) or not isinstance(password, str):
                errors.append(f"{item_path}: ssid and password must be strings")
                continue
            if not ssid and password:
                errors.append(f"{item_path}: password requires a non-empty ssid")
            if not ssid:
                continue
            active.append(ssid)
            if len(ssid.encode("utf-8")) > 32:
                errors.append(f"{item_path}.ssid: exceeds 32 UTF-8 bytes")
            if password and not 8 <= len(password.encode("utf-8")) <= 63:
                errors.append(f"{item_path}.password: expected 8-63 UTF-8 bytes or empty for open network")
            if any(ch in _WIFI_FORBIDDEN for ch in ssid + password):
                errors.append(f"{item_path}: contains a firmware build separator")
        if len(active) != len(set(active)):
            errors.append("wifi.networks: SSIDs must be unique")
    for section_name in ("services", "cloud"):
        section = secrets.get(section_name)
        if not isinstance(section, dict):
            errors.append(f"{section_name}: expected object")
        else:
            for key, value in section.items():
                if not isinstance(value, str):
                    errors.append(f"{section_name}.{key}: expected string")
    if enforce_permissions and path and path.exists() and not path.name.endswith(".example.json"):
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            errors.append(f"{path.name}: permissions must be 0600 or stricter, found {mode:04o}")
    return errors


def _legacy_layers(legacy: dict) -> tuple[dict, dict, list[str]]:
    normal: dict = {}
    private: dict = copy.deepcopy(DEFAULT_SECRETS)
    warnings: list[str] = []
    if not legacy:
        return normal, private, warnings
    known = {"wifi", "codex_member", "panel_display", "services", "cloud",
             "deepseek_token_tracking", "news_digest"}
    unknown = sorted(set(legacy) - known)
    if unknown:
        warnings.append("legacy unmapped fields retained in original file: " + ", ".join(unknown))
    for key in ("codex_member", "panel_display", "deepseek_token_tracking"):
        if isinstance(legacy.get(key), dict):
            value = copy.deepcopy(legacy[key])
            if key in ("codex_member", "deepseek_token_tracking") and value.get("valid_until_date") == "":
                value["valid_until_date"] = None
            if key == "deepseek_token_tracking" and value.get("initial_date") == "":
                value["initial_date"] = None
            normal[key] = value
    legacy_news = legacy.get("news_digest")
    if isinstance(legacy_news, dict) and "max_calls_per_issue" in legacy_news:
        normal["news_digest"] = {
            "max_calls_per_issue": copy.deepcopy(legacy_news.get("max_calls_per_issue"))
        }
    wifi = legacy.get("wifi")
    if isinstance(wifi, dict) and isinstance(wifi.get("networks"), list):
        private["wifi"]["networks"] = [
            {"ssid": str(row.get("ssid") or ""), "password": str(row.get("password") or "")}
            for row in wifi["networks"] if isinstance(row, dict) and row.get("ssid")
        ][:5]
    services = legacy.get("services")
    if isinstance(services, dict):
        for key in private["services"]:
            if key in services:
                private["services"][key] = str(services.get(key) or "")
    cloud = legacy.get("cloud")
    if isinstance(cloud, dict):
        for key in private["cloud"]:
            if key in cloud:
                private["cloud"][key] = str(cloud.get(key) or "")
    return normal, private, warnings


def _resolve_ref(secrets: dict, ref: str) -> Any:
    value: Any = secrets
    for part in ref.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _public_fingerprint(config: dict) -> str:
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _cache_key(config_path: Path, secrets_path: Path, legacy_path: Path) -> tuple[str, str, str]:
    return str(config_path.resolve()), str(secrets_path.resolve()), str(legacy_path.resolve())


def clear_last_known_good() -> None:
    """Test/support hook; production callers do not need to clear the cache."""
    with _CACHE_LOCK:
        _LAST_KNOWN_GOOD.clear()


def load_effective(*, config_path: Path | None = None, secrets_path: Path | None = None,
                   legacy_path: Path | None = None, use_last_known_good: bool = True) -> EffectiveConfig:
    config_path = Path(config_path or CONFIG_FILE)
    secrets_path = Path(secrets_path or SECRETS_FILE)
    legacy_path = Path(legacy_path or LEGACY_FILE)
    key = _cache_key(config_path, secrets_path, legacy_path)
    errors: list[str] = []
    warnings: list[str] = []
    sources = ["built-in defaults"]

    try:
        config_raw, secrets_raw, legacy_raw = _stable_pair_snapshot(
            config_path, secrets_path, legacy_path)
    except ConfigError as exc:
        with _CACHE_LOCK:
            previous = _LAST_KNOWN_GOOD.get(key)
            if use_last_known_good and previous is not None:
                result = previous.clone()
                result.degraded = True
                result.errors = list(exc.errors)
                result.warnings.append("configuration-pair recovery failed; last-known-good remains active")
                return result
        raise

    legacy, legacy_error = _decode_json(legacy_raw, legacy_path.name)
    if legacy_error not in (None, "missing"):
        errors.append(f"{legacy_path.name}: {legacy_error}")
    legacy_normal, legacy_secrets, legacy_warnings = _legacy_layers(legacy or {})
    warnings.extend(legacy_warnings)
    public = _merge(DEFAULT_CONFIG, legacy_normal)
    private = _merge(DEFAULT_SECRETS, legacy_secrets)
    if legacy:
        sources.append("legacy manual_settings.json")

    local, config_error = _decode_json(config_raw, config_path.name)
    if config_error not in (None, "missing"):
        errors.append(f"{config_path.name}: {config_error}")
    if local is not None:
        local_errors = _validate_public_document(local)
        errors.extend(local_errors)
        if not local_errors:
            if local.get("schema_version") == 1:
                warnings.append("inksight_config.json schema v1 upgraded in memory to v2; file unchanged")
            public = _merge(public, _upgrade_public_document(local))
            sources.append("inksight_config.json")

    local_secrets, secrets_error = _decode_json(secrets_raw, secrets_path.name)
    if secrets_error not in (None, "missing"):
        errors.append(f"{secrets_path.name}: {secrets_error}")
    if local_secrets is not None:
        upgraded_secrets = _upgrade_secrets_document(local_secrets)
        secret_errors = validate_secrets(upgraded_secrets, path=secrets_path)
        errors.extend(secret_errors)
        if not secret_errors:
            if local_secrets.get("schema_version") == 1:
                warnings.append("inksight_secrets.json schema v1 upgraded in memory to v2; file unchanged")
            private = _merge(private, upgraded_secrets)
            sources.append("inksight_secrets.json")

    errors.extend(validate_config(public))
    refs = public.get("secret_refs") or {}
    for name, ref in refs.items():
        resolved = _resolve_ref(private, ref) if isinstance(ref, str) else None
        if resolved is None:
            errors.append(f"secret_refs.{name}: {ref} does not resolve to a declared secret field")
        elif name == "wifi_networks" and not isinstance(resolved, list):
            errors.append(f"secret_refs.{name}: {ref} must resolve to a list")
        elif name != "wifi_networks" and not isinstance(resolved, str):
            errors.append(f"secret_refs.{name}: {ref} must resolve to a string")

    if errors:
        with _CACHE_LOCK:
            previous = _LAST_KNOWN_GOOD.get(key)
            if use_last_known_good and previous is not None:
                result = previous.clone()
                result.degraded = True
                result.errors = errors
                result.warnings.append("invalid candidate rejected; last-known-good remains active")
                return result
        raise ConfigError(errors)

    result = EffectiveConfig(public, private, sources, warnings, _public_fingerprint(public))
    with _CACHE_LOCK:
        _LAST_KNOWN_GOOD[key] = result.clone()
    return result


def secret_value(name: str, default: str = "", **paths: Any) -> str:
    effective = load_effective(**paths)
    ref = (effective.config.get("secret_refs") or {}).get(name)
    value = _resolve_ref(effective._secrets, ref) if isinstance(ref, str) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    env_name = _SECRET_ENV.get(name)
    if env_name and os.environ.get(env_name):
        return str(os.environ[env_name]).strip()
    return default


def wifi_networks(**paths: Any) -> list[dict]:
    effective = load_effective(**paths)
    ref = (effective.config.get("secret_refs") or {}).get("wifi_networks")
    value = _resolve_ref(effective._secrets, ref) if isinstance(ref, str) else None
    return copy.deepcopy(value) if isinstance(value, list) else []


def redacted_summary(effective: EffectiveConfig) -> dict:
    refs = effective.config.get("secret_refs") or {}
    secret_status = {}
    for name, ref in refs.items():
        value = _resolve_ref(effective._secrets, ref) if isinstance(ref, str) else None
        if isinstance(value, list):
            secret_status[name] = {"configured": bool(value), "count": len(value)}
        else:
            secret_status[name] = {"configured": bool(value)}
    summary = {
        "schema_version": effective.config.get("schema_version"),
        "fingerprint": effective.fingerprint,
        "sources": list(effective.sources),
        "degraded": effective.degraded,
        "warnings": list(effective.warnings),
        "errors": list(effective.errors or []),
        "effective": copy.deepcopy(effective.config),
        "secret_status": secret_status,
        "activation": {
            "backend": "runtime readers load the applied pair on demand; no backend restart is required for supported operator fields",
            "device_payload": "activity/page/time/display policy applies on the next successful device fetch supported by the current firmware",
            "device_compile": "Wi-Fi/cloud credentials and compile-time network values require a fresh build plus explicit flash; ordinary app flashing preserves NVS",
            "not_activated": "reserved sections remain rejected while enabled=true",
        },
    }
    member = summary["effective"].get("codex_member") or {}
    if member.get("valid_until_date"):
        member["valid_until_date"] = "<private-date>"
    tracking = summary["effective"].get("deepseek_token_tracking") or {}
    if tracking.get("initial_date"):
        tracking["initial_date"] = "<private-date>"
    return summary


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    key = str(path.resolve())
    with _CACHE_LOCK:
        lock = _FILE_LOCKS.setdefault(key, threading.RLock())
    with lock:
        lock_path = path.with_name(f".{path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:  # pragma: no cover
                if os.path.getsize(lock_path) == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:  # pragma: no cover
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)


def _atomic_replace(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
        try:
            dfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _fsync_parent(path: Path) -> None:
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _atomic_remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_parent(path)


def _blob(raw: bytes | None) -> str | None:
    return None if raw is None else base64.b64encode(raw).decode("ascii")


def _unblob(value: Any, field: str, expected_hash: Any) -> bytes | None:
    if value is None:
        if expected_hash is not None:
            raise ConfigError([f"configuration transaction journal: {field} hash is invalid"])
        return None
    if not isinstance(value, str) or not isinstance(expected_hash, str):
        raise ConfigError([f"configuration transaction journal: {field} is invalid"])
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError, binascii.Error):
        raise ConfigError([f"configuration transaction journal: {field} is invalid"])
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ConfigError([f"configuration transaction journal: {field} integrity check failed"])
    return raw


def _restore_blob(path: Path, raw: bytes | None, *, mode: int) -> None:
    if raw is None:
        _atomic_remove(path)
    else:
        _atomic_replace(path, raw, mode=mode)


def _recover_pair_locked(config_path: Path, secrets_path: Path, journal_path: Path) -> bool:
    """Complete or roll back one durable pair transaction. Caller owns pair lock."""
    if not journal_path.exists():
        return False
    journal, error = _read_json(journal_path)
    if error or journal is None:
        raise ConfigError([f"configuration transaction journal is unusable: {error or 'invalid'}"])
    if journal.get("journal_version") != 1 or journal.get("status") not in ("prepared", "committed"):
        raise ConfigError(["configuration transaction journal has an unsupported version or status"])
    side = "new" if journal["status"] == "committed" else "old"
    config_raw = _unblob(journal.get(f"{side}_config"), f"{side}_config",
                         journal.get(f"{side}_config_sha256"))
    secrets_raw = _unblob(journal.get(f"{side}_secrets"), f"{side}_secrets",
                          journal.get(f"{side}_secrets_sha256"))
    if (config_raw is None) != (secrets_raw is None):
        raise ConfigError(["configuration transaction journal contains a partial pair"])
    if config_raw is not None and secrets_raw is not None:
        config_value, config_error = _decode_json(config_raw, "journal config")
        secrets_value, secrets_error = _decode_json(secrets_raw, "journal secrets")
        errors: list[str] = []
        if config_error or config_value is None:
            errors.append(f"configuration transaction journal config: {config_error or 'invalid'}")
        else:
            errors.extend(_validate_public_document(config_value))
        if secrets_error or secrets_value is None:
            errors.append(f"configuration transaction journal secrets: {secrets_error or 'invalid'}")
        else:
            errors.extend(validate_secrets(secrets_value, enforce_permissions=False))
        if errors:
            raise ConfigError(errors)
    _restore_blob(Path(config_path), config_raw, mode=0o600)
    _restore_blob(Path(secrets_path), secrets_raw, mode=0o600)
    _atomic_remove(journal_path)
    return True


@contextmanager
def _pair_write_lock(config_path: Path, secrets_path: Path) -> Iterator[Path]:
    config_path, secrets_path = Path(config_path), Path(secrets_path)
    anchor, journal = _pair_names(config_path, secrets_path)
    with _pair_thread_lock(config_path, secrets_path):
        with _file_lock(anchor):
            _recover_pair_locked(config_path, secrets_path, journal)
            yield journal


def _backup_bytes(path: Path, raw: bytes | None, backup_dir: Path) -> None:
    if raw is None:
        return
    try:
        json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    digest = hashlib.sha256(raw).hexdigest()[:12]
    backup = backup_dir / f"{path.name}.{digest}.bak"
    if not backup.exists():
        _atomic_replace(backup, raw, mode=0o600)


def _commit_pair_locked(config_path: Path, secrets_path: Path, config_value: dict,
                        secrets_value: dict, *, journal_path: Path,
                        backup_dir: Path = BACKUP_DIR,
                        fault: Callable[[str], None] | None = None) -> bool:
    """Crash-recoverable two-file commit. Caller must own ``_pair_write_lock``.

    ``fault`` exists solely for deterministic failure tests. The prepared
    journal contains both old and new bytes, is private (0600), and is durable
    before either public target changes.
    """
    config_path, secrets_path = Path(config_path), Path(secrets_path)
    config_errors = _validate_public_document(config_value)
    secret_errors = validate_secrets(secrets_value, enforce_permissions=False)
    if config_errors or secret_errors:
        raise ConfigError(config_errors + secret_errors)
    config_new = (json.dumps(config_value, ensure_ascii=False, indent=2,
                             allow_nan=False) + "\n").encode("utf-8")
    secrets_new = (json.dumps(secrets_value, ensure_ascii=False, indent=2,
                              allow_nan=False) + "\n").encode("utf-8")
    config_old, secrets_old = _file_bytes(config_path), _file_bytes(secrets_path)
    if config_old == config_new and secrets_old == secrets_new:
        return False
    _backup_bytes(config_path, config_old, Path(backup_dir))
    _backup_bytes(secrets_path, secrets_old, Path(backup_dir))
    journal = {
        "journal_version": 1,
        "transaction_id": os.urandom(12).hex(),
        "status": "prepared",
        "old_config": _blob(config_old),
        "old_config_sha256": hashlib.sha256(config_old).hexdigest() if config_old is not None else None,
        "old_secrets": _blob(secrets_old),
        "old_secrets_sha256": hashlib.sha256(secrets_old).hexdigest() if secrets_old is not None else None,
        "new_config": _blob(config_new),
        "new_config_sha256": hashlib.sha256(config_new).hexdigest(),
        "new_secrets": _blob(secrets_new),
        "new_secrets_sha256": hashlib.sha256(secrets_new).hexdigest(),
    }
    _atomic_replace(journal_path, (json.dumps(journal, sort_keys=True) + "\n").encode(), mode=0o600)
    try:
        if fault:
            fault("prepared")
        _atomic_replace(config_path, config_new, mode=0o600)
        if fault:
            fault("after_config")
        _atomic_replace(secrets_path, secrets_new, mode=0o600)
        if fault:
            fault("after_secrets")
        journal["status"] = "committed"
        _atomic_replace(journal_path, (json.dumps(journal, sort_keys=True) + "\n").encode(), mode=0o600)
        if fault:
            fault("after_commit")
        _atomic_remove(journal_path)
    except Exception:
        # Ordinary failures are rolled back before returning. A process crash
        # bypasses this handler and is repaired by the next effective read.
        _recover_pair_locked(config_path, secrets_path, journal_path)
        raise
    return True


def atomic_write_json(path: Path, value: dict, *, backup_dir: Path | None = None,
                      mode: int = 0o600) -> bool:
    """Lock + fsync + same-directory replace; valid prior content gets a persistent backup."""
    path = Path(path)
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    with _file_lock(path):
        if path.exists() and path.read_bytes() == encoded:
            return False
        if path.exists():
            previous = path.read_bytes()
            try:
                json.loads(previous.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            else:
                target_dir = Path(backup_dir or BACKUP_DIR)
                target_dir.mkdir(parents=True, exist_ok=True)
                os.chmod(target_dir, 0o700)
                digest = hashlib.sha256(previous).hexdigest()[:12]
                backup = target_dir / f"{path.name}.{digest}.bak"
                if not backup.exists():
                    _atomic_replace(backup, previous, mode=0o600)
        _atomic_replace(path, encoded, mode=mode)
        return True


def atomic_update_secrets(updater: Callable[[dict], None], *,
                          config_path: Path = CONFIG_FILE,
                          secrets_path: Path = SECRETS_FILE,
                          backup_dir: Path = BACKUP_DIR) -> dict:
    """Update secrets through the same durable pair transaction used by migration."""
    config_path, secrets_path = Path(config_path), Path(secrets_path)
    with _pair_write_lock(config_path, secrets_path) as journal:
        config_value, config_error = _decode_json(_file_bytes(config_path), config_path.name)
        secrets_value, secrets_error = _decode_json(_file_bytes(secrets_path), secrets_path.name)
        errors: list[str] = []
        if config_error or config_value is None:
            errors.append(f"configuration pair update refused: public config {config_error or 'invalid'}")
        else:
            errors.extend(_validate_public_document(config_value))
        if secrets_error or secrets_value is None:
            errors.append(f"configuration pair update refused: secrets {secrets_error or 'invalid'}")
        else:
            errors.extend(validate_secrets(secrets_value, path=secrets_path))
        if errors:
            raise ConfigError(errors)
        assert config_value is not None and secrets_value is not None
        updated = copy.deepcopy(secrets_value)
        updater(updated)
        errors = validate_secrets(updated, enforce_permissions=False)
        if errors:
            raise ConfigError(errors)
        _commit_pair_locked(config_path, secrets_path, config_value, updated,
                            journal_path=journal, backup_dir=Path(backup_dir))
        result = copy.deepcopy(updated)
    clear_last_known_good()
    return result


def atomic_replace_pair(config_value: dict, secrets_value: dict, *,
                        config_path: Path | None = None,
                        secrets_path: Path | None = None,
                        backup_dir: Path | None = None) -> bool:
    """Validate and durably replace the public/private operator-config pair.

    This is the single write boundary used by the local management console.
    Both documents are committed through the existing crash-recovery journal,
    so readers can never observe a half-updated pair.  Secret values are never
    returned to the caller.
    """
    config_path = Path(config_path or CONFIG_FILE)
    secrets_path = Path(secrets_path or SECRETS_FILE)
    backup_dir = Path(backup_dir or BACKUP_DIR)
    public = copy.deepcopy(config_value)
    private = copy.deepcopy(secrets_value)
    errors = _validate_public_document(public)
    errors.extend(validate_secrets(private, enforce_permissions=False))
    if errors:
        raise ConfigError(errors)
    with _pair_write_lock(config_path, secrets_path) as journal:
        changed = _commit_pair_locked(
            config_path,
            secrets_path,
            public,
            private,
            journal_path=journal,
            backup_dir=backup_dir,
        )
    clear_last_known_good()
    return changed


def atomic_update_json(path: Path, updater: Callable[[dict], None], *, default: dict,
                       backup_dir: Path | None = None) -> dict:
    path = Path(path)
    with _file_lock(path):
        current, error = _read_json(path)
        if error == "missing":
            current = copy.deepcopy(default)
        elif error or current is None:
            raise ConfigError([f"{path.name}: {error or 'invalid'}"])
        updater(current)
        encoded = (json.dumps(current, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
        if path.exists():
            previous = path.read_bytes()
            target_dir = Path(backup_dir or BACKUP_DIR)
            target_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(target_dir, 0o700)
            digest = hashlib.sha256(previous).hexdigest()[:12]
            backup = target_dir / f"{path.name}.{digest}.bak"
            if not backup.exists():
                _atomic_replace(backup, previous, mode=0o600)
        _atomic_replace(path, encoded, mode=0o600)
        return copy.deepcopy(current)


def init_files(*, config_path: Path = CONFIG_FILE, secrets_path: Path = SECRETS_FILE,
               legacy_path: Path = LEGACY_FILE) -> dict:
    """Initialize only a genuinely new installation.

    A legacy user is directed to migration because writing full defaults and
    empty secrets would otherwise shadow valid legacy values. A partial new
    pair is never filled in automatically.
    """
    config_path, secrets_path, legacy_path = map(Path, (config_path, secrets_path, legacy_path))
    with _pair_write_lock(config_path, secrets_path) as journal:
        config_exists, secrets_exists = config_path.exists(), secrets_path.exists()
        if config_exists != secrets_exists:
            raise ConfigError([
                "initialization refused: only one versioned configuration file exists; "
                "repair or remove the partial pair, then validate explicitly"
            ])
        if config_exists and secrets_exists:
            current_config, config_error = _read_json(config_path)
            current_secrets, secrets_error = _read_json(secrets_path)
            errors: list[str] = []
            if config_error or current_config is None:
                errors.append(f"initialization refused: public config {config_error or 'invalid'}")
            else:
                errors.extend(_validate_public_document(current_config))
            if secrets_error or current_secrets is None:
                errors.append(f"initialization refused: secrets {secrets_error or 'invalid'}")
            else:
                errors.extend(validate_secrets(current_secrets, path=secrets_path))
            if errors:
                raise ConfigError(errors)
            return {"created": [],
                    "skipped_existing": [str(config_path), str(secrets_path)],
                    "status": "already-initialized"}
        if legacy_path.exists():
            raise ConfigError([
                "initialization refused: legacy manual_settings.json exists; "
                "run migrate --dry-run so existing values are preserved"
            ])
        _commit_pair_locked(config_path, secrets_path, copy.deepcopy(DEFAULT_CONFIG),
                            copy.deepcopy(DEFAULT_SECRETS), journal_path=journal)
        return {"created": [str(config_path), str(secrets_path)],
                "skipped_existing": [], "status": "initialized-new-user"}


def _changed_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        out = []
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in before or key not in after:
                out.append(path)
            else:
                out.extend(_changed_paths(before[key], after[key], path))
        return out
    return [] if before == after else [prefix]


def _redacted_secret_paths(before: dict, after: dict) -> list[str]:
    safe: set[str] = set()
    for path in _changed_paths(before, after):
        if path.startswith("wifi.networks"):
            safe.add("wifi.networks")
        elif path.startswith("services."):
            safe.add(".".join(path.split(".")[:2]))
        elif path.startswith("cloud."):
            safe.add(".".join(path.split(".")[:2]))
        elif path:
            safe.add(path)
    return sorted(safe)


def _migration_existing_pair(config_path: Path, secrets_path: Path,
                             proposed_config: dict, proposed_secrets: dict) -> tuple[str, dict]:
    config_raw, secrets_raw = _file_bytes(config_path), _file_bytes(secrets_path)
    if (config_raw is None) != (secrets_raw is None):
        raise ConfigError([
            "migration conflict: only one target file exists; no file was overwritten"
        ])
    if config_raw is None:
        return "first-migration", {
            "config": _changed_paths({}, proposed_config),
            "secrets": _redacted_secret_paths({}, proposed_secrets),
        }
    current_config, config_error = _decode_json(config_raw, config_path.name)
    current_secrets, secrets_error = _decode_json(secrets_raw, secrets_path.name)
    errors: list[str] = []
    if config_error or current_config is None:
        errors.append(f"migration conflict: existing config is unusable: {config_error or 'invalid'}")
    else:
        local_errors = _validate_public_document(current_config)
        errors.extend(f"migration conflict: existing config: {item}" for item in local_errors)
    if secrets_error or current_secrets is None:
        errors.append(f"migration conflict: existing secrets are unusable: {secrets_error or 'invalid'}")
    else:
        current_secrets = _upgrade_secrets_document(current_secrets)
        errors.extend(
            f"migration conflict: existing secrets: {item}"
            for item in validate_secrets(current_secrets, path=secrets_path))
    if errors:
        raise ConfigError(errors)
    assert current_config is not None and current_secrets is not None
    current_config = _upgrade_public_document(current_config)
    effective_current_config = _merge(proposed_config, current_config)
    effective_current_secrets = _merge(proposed_secrets, current_secrets)
    public_conflicts = _changed_paths(effective_current_config, proposed_config)
    secret_conflicts = _redacted_secret_paths(effective_current_secrets, proposed_secrets)
    if public_conflicts or secret_conflicts:
        paths = [f"config:{path}" for path in public_conflicts]
        paths.extend(f"secrets:{path}" for path in secret_conflicts)
        raise ConfigError([
            "migration conflict: existing versioned files differ from the legacy proposal; "
            "no overwrite performed; paths=" + ", ".join(paths)
        ])
    return "already-migrated", {"config": [], "secrets": []}


def migrate_legacy(*, legacy_path: Path = LEGACY_FILE, config_path: Path = CONFIG_FILE,
                   secrets_path: Path = SECRETS_FILE, apply: bool = False,
                   confirm_activate: bool = False, backup_dir: Path = BACKUP_DIR,
                   _fault: Callable[[str], None] | None = None) -> dict:
    legacy_path, config_path, secrets_path = map(Path, (legacy_path, config_path, secrets_path))
    with _pair_write_lock(config_path, secrets_path) as journal:
        legacy_raw = _file_bytes(legacy_path)
        legacy, error = _decode_json(legacy_raw, legacy_path.name)
        if error:
            raise ConfigError([f"{legacy_path.name}: {error}"])
        normal, private, warnings = _legacy_layers(legacy or {})
        proposed_config = _merge(DEFAULT_CONFIG, normal)
        proposed_secrets = _merge(DEFAULT_SECRETS, private)
        errors = validate_config(proposed_config) + validate_secrets(
            proposed_secrets, enforce_permissions=False)
        if errors:
            raise ConfigError(errors)
        status, would_change = _migration_existing_pair(
            config_path, secrets_path, proposed_config, proposed_secrets)
        result = {
            "dry_run": not apply,
            "status": status,
            "would_change": would_change,
            "secret_values": "redacted",
            "warnings": warnings,
            "legacy_preserved": True,
            "state_untouched": True,
        }
        if not apply:
            return result
        if status == "already-migrated":
            result["changed"] = {"config": False, "secrets": False}
            result["dry_run"] = False
            return result
        if not confirm_activate:
            raise ConfigError(["migration apply requires --confirm-activate; dry-run is the safe default"])
        if _fault:
            _fault("after_validation")
        if _file_bytes(legacy_path) != legacy_raw:
            raise ConfigError(["migration aborted: legacy source changed during validation"])
        rechecked_status, _ = _migration_existing_pair(
            config_path, secrets_path, proposed_config, proposed_secrets)
        if rechecked_status != "first-migration":
            raise ConfigError(["migration aborted: target files changed during validation; no overwrite performed"])
        changed = _commit_pair_locked(
            config_path, secrets_path, proposed_config, proposed_secrets,
            journal_path=journal, backup_dir=Path(backup_dir), fault=_fault)
        result["changed"] = {"config": changed, "secrets": changed}
        result["dry_run"] = False
        return result
