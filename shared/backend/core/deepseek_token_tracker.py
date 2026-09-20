"""Daily DeepSeek token accounting for the two InkSight panels.

Exact ``usage`` returned by InkSight calls is accumulated directly.  Balance
drops are converted to a conservative token estimate only after the configured
threshold is reached.  The panel displays the larger of both values.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

from . import manual_settings
from . import state_store


_BJ = timezone(timedelta(hours=8))
_FILE = state_store.state_path("deepseek_token_usage.json")


def configure_file(path) -> None:
    global _FILE
    _FILE = Path(path)


def _day(at: float) -> str:
    return datetime.fromtimestamp(at, tz=_BJ).strftime("%Y-%m-%d")


def _manual_seed(day: str) -> dict:
    cfg = manual_settings.token_tracking()
    if str(cfg.get("initial_date") or "") != day:
        return {"total": 0, "prompt": 0, "completion": 0, "complete": True}
    return {
        "total": max(0, int(cfg.get("initial_tokens") or 0)),
        "prompt": max(0, int(cfg.get("initial_prompt_tokens") or 0)),
        "completion": max(0, int(cfg.get("initial_completion_tokens") or 0)),
        "complete": bool(cfg.get("initial_complete", False)),
    }


def _new_day(day: str) -> dict:
    seed = _manual_seed(day)
    return {
        "date": day,
        "actual_tokens": seed["total"],
        "actual_prompt_tokens": seed["prompt"],
        "actual_completion_tokens": seed["completion"],
        "actual_complete": seed["complete"],
        "inferred_tokens": 0,
        "pending_balance_cost_cny": 0.0,
        "last_balance_cny": None,
        "last_source": "manual-seed" if seed["total"] else "none",
    }


def _roll(data: dict, day: str) -> None:
    if data.get("date") != day:
        data.clear()
        data.update(_new_day(day))


def _usage_value(usage, key: str) -> int:
    if isinstance(usage, dict):
        value = usage.get(key)
    else:
        value = getattr(usage, key, None)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def record_usage(usage, *, source: str = "inksight", at: float | None = None) -> dict:
    """Add one API response's exact token counts and return today's snapshot."""
    stamp = float(time.time() if at is None else at)
    day = _day(stamp)
    prompt = _usage_value(usage, "prompt_tokens")
    completion = _usage_value(usage, "completion_tokens")
    total = _usage_value(usage, "total_tokens")
    if total <= 0:
        total = prompt + completion
    if total <= 0:
        return snapshot(at=stamp)

    def update(data: dict) -> None:
        _roll(data, day)
        data["actual_tokens"] = int(data.get("actual_tokens") or 0) + total
        data["actual_prompt_tokens"] = int(data.get("actual_prompt_tokens") or 0) + prompt
        data["actual_completion_tokens"] = int(data.get("actual_completion_tokens") or 0) + completion
        data["last_source"] = source

    state_store.update_json(_FILE, update, default=_new_day(day))
    return snapshot(at=stamp)


def _is_peak(at: float) -> bool:
    now = datetime.fromtimestamp(at, tz=_BJ)
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    return 9 * 60 <= minute < 12 * 60 or 14 * 60 <= minute < 18 * 60


def _blended_rate(data: dict, at: float) -> float:
    cfg = manual_settings.token_tracking()
    prompt = int(data.get("actual_prompt_tokens") or 0)
    completion = int(data.get("actual_completion_tokens") or 0)
    measured = prompt + completion
    if measured > 0:
        output_ratio = completion / measured
    else:
        output_ratio = float(cfg.get("balance_estimate_output_ratio") or 0.10)
    output_ratio = min(1.0, max(0.0, output_ratio))
    if _is_peak(at):
        input_rate, output_rate = 3.0, 9.0
    else:
        input_rate, output_rate = 1.5, 4.5
    return input_rate * (1.0 - output_ratio) + output_rate * output_ratio


def observe_balance(balance_cny, *, at: float | None = None) -> dict:
    """Observe a confirmed CNY balance and update the thresholded estimate."""
    try:
        current = float(balance_cny)
    except (TypeError, ValueError):
        return snapshot(at=at)
    if current < 0:
        return snapshot(at=at)
    stamp = float(time.time() if at is None else at)
    day = _day(stamp)
    cfg = manual_settings.token_tracking()
    threshold = max(0.01, float(cfg.get("balance_refresh_threshold_cny") or 0.10))

    def update(data: dict) -> None:
        _roll(data, day)
        previous = data.get("last_balance_cny")
        data["last_balance_cny"] = round(current, 6)
        if previous is None:
            return
        delta = float(previous) - current
        if delta < 0:
            data["pending_balance_cost_cny"] = 0.0
            data["last_source"] = "balance-recharge"
            return
        if delta == 0:
            return
        pending = float(data.get("pending_balance_cost_cny") or 0.0) + delta
        data["pending_balance_cost_cny"] = round(pending, 6)
        if pending + 1e-9 < threshold:
            return
        rate = _blended_rate(data, stamp)
        inferred = int(round(pending * 1_000_000.0 / rate)) if rate > 0 else 0
        data["inferred_tokens"] = int(data.get("inferred_tokens") or 0) + max(0, inferred)
        data["pending_balance_cost_cny"] = 0.0
        data["last_source"] = "balance-estimate"

    state_store.update_json(_FILE, update, default=_new_day(day))
    return snapshot(at=stamp)


def snapshot(*, at: float | None = None) -> dict:
    stamp = float(time.time() if at is None else at)
    day = _day(stamp)
    raw, error = state_store.read_json(_FILE)
    data = raw if not error and isinstance(raw, dict) else _new_day(day)
    if data.get("date") != day:
        data = _new_day(day)
    actual = max(0, int(data.get("actual_tokens") or 0))
    inferred = max(0, int(data.get("inferred_tokens") or 0))
    total = max(actual, inferred)
    return {
        "date": day,
        "tokens": total,
        "actual_tokens": actual,
        "inferred_tokens": inferred,
        "complete": bool(data.get("actual_complete", True)),
        "source": "actual" if actual >= inferred else "balance-estimate",
        "pending_balance_cost_cny": float(data.get("pending_balance_cost_cny") or 0.0),
    }

