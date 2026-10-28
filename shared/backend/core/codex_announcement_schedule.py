"""Pure, independently configurable announcement cadence, never quota cadence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

BJ = timezone(timedelta(hours=8))
DEFAULTS = {
    "enabled": False,
    "target_handle": "thsottiaux",
    "target_user_id": "1953337039510003712",
    "active_minutes": 60, "light_minutes": 120,
    "low_active_minutes": 30, "low_light_minutes": 60,
    "low_remaining_percent": 30,
    "near_window_minutes": 120, "near_interval_minutes": 15,
    "urgent_window_minutes": 30, "urgent_interval_minutes": 10,
    "night_slots": ["00:00", "06:00"], "overnight_catchup_until": "06:00",
    "overlap_hours": 24, "overlap_recheck_minutes": 360,
    "page_size": 20, "max_pages": 2, "timeout_seconds": 8,
    "max_post_age_days": 7, "max_forecast_days": 7,
    "plan_max_age_seconds": 86400,
    "retry_initial_seconds": 300, "retry_max_seconds": 21600,
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
    for key in ('whenreset_enabled','nextreset_enabled'):
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


def interval_seconds(cfg: dict, mode: str, remaining, events: dict, now: float) -> int | None:
    if mode not in {"active", "light"}:
        return None
    low = type(remaining) in (int, float) and 0 <= remaining <= cfg["low_remaining_percent"]
    base = cfg[("low_" if low else "") + mode + "_minutes"] * 60
    nearest = min((e["target_at"] - now for e in events.values() if type(e.get("target_at")) in (int,float) and e["target_at"] > now), default=None)
    if nearest is not None:
        if nearest <= cfg["urgent_window_minutes"] * 60:
            base = min(base, cfg["urgent_interval_minutes"] * 60)
        elif nearest <= cfg["near_window_minutes"] * 60:
            base = min(base, cfg["near_interval_minutes"] * 60)
    return base


def relevant_slots(cfg: dict, now: float) -> list[str]:
    """Late startup only catches the current overnight slot, not missed days."""
    local = datetime.fromtimestamp(now, BJ)
    date = local.strftime("%Y-%m-%d")
    minute = local.hour * 60 + local.minute
    cutoff = sum(int(x) * k for x, k in zip(cfg["overnight_catchup_until"].split(":"), (60, 1)))
    result = []
    for slot in cfg["night_slots"]:
        h, m = (int(x) for x in slot.split(":"))
        at = h * 60 + m
        # The first overnight slot remains eligible until the catch-up cutoff.
        # Other exact-time slots have a 90-second timer/coalescing grace.
        inside = at <= minute < cutoff if at == 0 else (0 <= (local.hour * 3600 + local.minute * 60 + local.second) - at * 60 < 90)
        if inside:
            result.append(date + "@" + slot)
    return result


def decision(state: dict, cfg: dict, *, now: float, mode: str, remaining=None,
             events: dict | None = None) -> dict:
    interval = interval_seconds(cfg, mode, remaining, events or {}, now)
    slots = [s for s in relevant_slots(cfg, now) if s not in (state.get("slot_success") or {})]
    # Failed exact-time work remains pending through its bounded backoff even
    # after the 90s trigger grace. Never backfill a previous Beijing date.
    date = datetime.fromtimestamp(now, BJ).strftime("%Y-%m-%d")
    slots = sorted(set(slots + [s for s in state.get("pending_slots", [])
                               if isinstance(s, str) and s.startswith(date + "@")
                               and s not in (state.get("slot_success") or {})]))
    last = state.get("last_success_at") or 0
    due = bool(slots or (interval is not None and (not last or now - last >= interval)))
    blocked = max(state.get("next_allowed_at") or 0, state.get("inflight_until") or 0)
    return {"due": due and now >= blocked, "slots": slots,
            "interval_seconds": interval, "next_allowed_at": blocked,
            "reason": "backoff" if now < blocked else "night_slot" if slots else "interval" if due else "not_due",
            # A just-completed pre-boundary check can satisfy a simultaneous
            # slot without making a second HTTP operation.
            "coalesce": bool(slots and last and 0 <= now - last < 60)}
