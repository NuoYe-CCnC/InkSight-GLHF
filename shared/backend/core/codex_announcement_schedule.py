"""Pure, independently configurable announcement cadence, never quota cadence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

BJ = timezone(timedelta(hours=8))
DEFAULTS = {
    "enabled": False,
    "analysis_enabled": False,
    "target_handle": "thsottiaux",
    "target_user_id": "1953337039510003712",
    "active_minutes": 60, "light_minutes": 120,
    "low_active_minutes": 30, "low_light_minutes": 60,
    "low_remaining_percent": 30,
    "near_window_minutes": 120, "near_interval_minutes": 15,
    "urgent_window_minutes": 30, "urgent_interval_minutes": 10,
    "night_slots": ["06:00"], "overnight_catchup_until": "06:00",
    "overlap_hours": 24, "overlap_recheck_minutes": 360,
    "page_size": 20, "max_pages": 2, "timeout_seconds": 8,
    "max_post_age_days": 7, "max_forecast_days": 7,
    "plan_max_age_seconds": 86400,
    "retry_initial_seconds": 900, "retry_max_seconds": 10800,
    "forbidden_backoff_seconds": 86400,
    "whenreset_enabled": False, "nextreset_enabled": False,
    "auxiliary_interval_minutes": 120, "auxiliary_timeout_seconds": 6,
    "auxiliary_max_links": 8,
}
_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def validate(config: dict, errors: list[str]) -> None:
    section = config.get("codex_announcements")
    prefix = "codex_announcements"
    if not isinstance(section, dict):
        errors.append(prefix + ": expected object"); return
    if not isinstance(section.get("enabled"), bool):
        errors.append(prefix + ".enabled: expected true or false")
    for key in ('whenreset_enabled','nextreset_enabled','analysis_enabled'):
        if not isinstance(section.get(key),bool): errors.append(prefix+'.'+key+': expected true or false')
    if not isinstance(section.get("target_handle"), str) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", section.get("target_handle", "")):
        errors.append(prefix + ".target_handle: expected public handle, no URL or @")
    if not isinstance(section.get("target_user_id"), str) or not re.fullmatch(r"[0-9]{5,24}", section.get("target_user_id", "")):
        errors.append(prefix + ".target_user_id: expected stable public numeric user id")
    ranges = {key: (10, 1440) for key in ("active_minutes", "light_minutes", "low_active_minutes", "low_light_minutes")}
    ranges.update(low_remaining_percent=(0, 100), near_window_minutes=(30, 720),
                  auxiliary_interval_minutes=(120,1440),auxiliary_timeout_seconds=(2,10),auxiliary_max_links=(1,12),
                  near_interval_minutes=(10, 120), urgent_window_minutes=(1, 120),
                  urgent_interval_minutes=(10, 60), overlap_hours=(1, 72),
                  overlap_recheck_minutes=(60, 1440), page_size=(1, 50), max_pages=(1, 2),
                  timeout_seconds=(2, 15), max_post_age_days=(1, 14), max_forecast_days=(1, 14),
                  plan_max_age_seconds=(1800, 172800), retry_initial_seconds=(60, 3600),
                  retry_max_seconds=(3600, 86400), forbidden_backoff_seconds=(21600, 604800))
    for key, (low, high) in ranges.items():
        value = section.get(key)
        if type(value) is not int or not low <= value <= high:
            errors.append(f"{prefix}.{key}: expected integer {low}..{high}")
    slots = section.get("night_slots")
    if (not isinstance(slots, list) or not 1 <= len(slots) <= 4
            or any(not isinstance(s, str) or not _TIME.fullmatch(s) for s in slots)
            or len(slots) != len(set(slots))):
        errors.append(prefix + ".night_slots: expected 1..4 unique HH:MM slots")
    if not isinstance(section.get("overnight_catchup_until"), str) or not _TIME.fullmatch(section.get("overnight_catchup_until", "")):
        errors.append(prefix + ".overnight_catchup_until: expected HH:MM")
    for a, b in (("urgent_window_minutes", "near_window_minutes"),
                 ("urgent_interval_minutes", "near_interval_minutes"),
                 ("retry_initial_seconds", "retry_max_seconds"),
                 ("low_active_minutes", "active_minutes"),
                 ("low_light_minutes", "light_minutes")):
        if type(section.get(a)) is int and type(section.get(b)) is int and section[a] > section[b]:
            errors.append(f"{prefix}.{a}: must not exceed {b}")


def mode_at(config: dict, now: float, *, is_workday=None) -> str:
    if is_workday is None:
        from .news_calendar import is_workday
    local = datetime.fromtimestamp(now, BJ)
    day = "workday" if is_workday(local) else "restday"
    minute = local.hour * 60 + local.minute
    rows = ((config.get("device_policy") or {}).get("activity_windows") or {}).get(day, [])
    for row in rows:
        start, end = (sum(int(n) * k for n, k in zip(row[key].split(":"), (60, 1))) for key in ("start", "end"))
        if (start <= minute < end) if start < end else (minute >= start or minute < end):
            return row["mode"]
    return "night"  # Invalid/gapped policy must not silently enable daytime traffic.


def interval_seconds(cfg, mode, remaining, events, now):
    return None  # Legacy mode/quota cadence intentionally retired.


def relevant_slots(cfg, now):
    from .automation_schedule import daily_slot
    return [daily_slot(now)[0]]


def decision(state, cfg, *, now, mode=None, remaining=None, events=None, manual=False):
    from .automation_schedule import announcement_due
    return announcement_due(state, now, manual)
