"""One immutable daily USD reference, shared by both gold conversions.

Schema 2 deliberately discards schema-1 CNY and nearest-midnight references.
Historical fallback means the earliest *available* valid same-day sampler
point, not the market's first trade. FX is never frozen into the baseline.
"""
from __future__ import annotations

import copy
import json
import math
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from . import state_store

PROVIDER = "xaus.com"
INSTRUMENT = "XAU"
SCHEMA = 2
GRAMS_PER_TROY_OUNCE = 31.1034768
INTRADAY_ENDPOINT = "https://xaus.com/api/v1/intraday?symbol=xau&hours=48"
STATE_FILE = state_store.state_path("xaus_gold_baselines.json")
DEFAULT_TOLERANCE_SECONDS = 120  # legacy config accepted; never a pre-midnight tolerance
DEFAULT_RECOVERY_COOLDOWN_SECONDS = 21600
DEFAULT_RECOVERY_MAX_ATTEMPTS = 2
MAX_DAYS = 45
_BJ = timezone(timedelta(hours=8))


def configure_state_file(path) -> None:
    global STATE_FILE
    STATE_FILE = Path(path)


def _default_state() -> dict:
    return {"schema": SCHEMA, "timezone": "Asia/Shanghai", "records": {}, "recoveries": {}}


def _load_locked() -> dict:
    value, error = state_store._decode(STATE_FILE)
    result = _default_state()
    if not error and isinstance(value, dict):
        # Keep request accounting on migration, not the incompatible old references.
        recoveries = value.get("recoveries")
        if isinstance(recoveries, dict):
            result["recoveries"] = copy.deepcopy(recoveries)
        if value.get("schema") == SCHEMA and value.get("timezone") == "Asia/Shanghai":
            records = value.get("records")
            if isinstance(records, dict):
                result["records"] = copy.deepcopy(records)
        else:
            result["migration_from"] = value.get("schema")
    elif error and error != "missing":
        # A corrupt whole file cannot safely reset persistent request budgets.
        result["last_state_error"] = error
    return result


def _save_locked(value: dict) -> None:
    clean = copy.deepcopy(value)
    clean.pop("last_state_error", None)
    for key in ("records", "recoveries"):
        clean[key] = dict(sorted((clean.get(key) or {}).items())[-MAX_DAYS:])
    state_store._write_locked(STATE_FILE, clean, meta=True, keep_backup=True)


def _finite_positive(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _epoch(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _strict_int(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _iso_epoch(value) -> Optional[int]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return int(parsed.timestamp()) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _day_text(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, _BJ).date().isoformat()


def _midnight_epoch(day_text: str) -> int:
    day = date.fromisoformat(day_text)
    return int(datetime(day.year, day.month, day.day, tzinfo=_BJ).timestamp())


def _config() -> tuple[int, int, int]:
    try:
        from .operator_config import load_effective
        gold = load_effective().config.get("gold_refresh") or {}
        wait = int(gold.get("midnight_tolerance_seconds", DEFAULT_TOLERANCE_SECONDS))
        cooldown = int(gold.get("intraday_recovery_cooldown_seconds", DEFAULT_RECOVERY_COOLDOWN_SECONDS))
        attempts = int(gold.get("intraday_recovery_max_attempts_per_day", DEFAULT_RECOVERY_MAX_ATTEMPTS))
        return max(0, min(wait, 600)), max(1800, min(cooldown, 86400)), max(1, min(attempts, 8))
    except Exception:  # noqa: BLE001
        return DEFAULT_TOLERANCE_SECONDS, DEFAULT_RECOVERY_COOLDOWN_SECONDS, DEFAULT_RECOVERY_MAX_ATTEMPTS


def _valid_record(row, day_text: str, *, now: float | None = None) -> bool:
    if not isinstance(row, dict):
        return False
    usd = row.get("usd")
    if not isinstance(usd, dict):
        return False
    stamp = _epoch(usd.get("price_as_of"))
    midnight = _midnight_epoch(day_text)
    return (row.get("schema") == SCHEMA and row.get("date") == day_text
            and row.get("timezone") == "Asia/Shanghai" and row.get("provider") == PROVIDER
            and row.get("instrument_id") == INSTRUMENT and row.get("frozen") is True
            and row.get("method") in {"spot_exact_midnight", "intraday_earliest_available"}
            and usd.get("currency") == "USD" and usd.get("unit") == "troy_oz"
            and _finite_positive(usd.get("value")) is not None and stamp is not None
            and midnight <= stamp < midnight + 86400
            and row.get("price_as_of") == stamp
            and row.get("reference_kind") == ("midnight" if stamp == midnight else "first_available")
            and (row.get("method") != "spot_exact_midnight" or stamp == midnight)
            and (now is None or stamp <= int(now)))


def _record(price: float, stamp: int, day_text: str, source: str, method: str, now: float) -> dict:
    midnight = _midnight_epoch(day_text)
    return {"schema": SCHEMA, "date": day_text, "timezone": "Asia/Shanghai",
            "midnight_at": midnight, "distance_seconds": stamp - midnight,
            "provider": PROVIDER, "instrument_id": INSTRUMENT, "method": method,
            "reference_kind": "midnight" if stamp == midnight else "first_available",
            "price_as_of": stamp, "usd": {"value": price, "currency": "USD",
            "unit": "troy_oz", "source": source, "price_as_of": stamp},
            "frozen": True, "complete": True, "frozen_at": int(now)}


def consider_spot(item: dict, *, now: float | None = None,
                  tolerance_seconds: int | None = None) -> bool:
    """Only an exact same-day 00:00 quote can seed B without history."""
    t = float(time.time() if now is None else now)
    day_text = _day_text(t)
    stamp = _epoch(item.get("price_as_of"))
    price = _finite_positive(item.get("spot_usd_oz"))
    if (item.get("provider") != PROVIDER or item.get("instrument_id") != INSTRUMENT
            or item.get("calculation_schema") != SCHEMA or price is None
            or stamp != _midnight_epoch(day_text) or stamp > t
            or item.get("usd_status") != "fresh"):
        return False
    with state_store.file_lock(STATE_FILE, operation="xaus-midnight-baseline"):
        state = _load_locked()
        if state.get("last_state_error") or _valid_record(state["records"].get(day_text), day_text, now=t):
            return False
        state["records"][day_text] = _record(price, stamp, day_text,
            str(item.get("price_source") or "unknown"), "spot_exact_midnight", t)
        _save_locked(state)
    return True


def _request_intraday(timeout_sec: float = 12) -> tuple[int, Optional[dict]]:
    request = urllib.request.Request(INTRADAY_ENDPOINT, headers={
        "Accept": "application/json", "User-Agent": "InkSight/3.0 (+https://xaus.com/api)"})
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:
        status = int(getattr(response, "status", 200))
        body = response.read()
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None
    return status, value if isinstance(value, dict) else None


def _recovery_reason(state: dict, t: float) -> str | None:
    day_text = _day_text(t)
    wait, cooldown, max_attempts = _config()
    if state.get("last_state_error"):
        return "state_unavailable"
    if _valid_record(state["records"].get(day_text), day_text, now=t):
        return "usd_baseline_exists"
    if t < _midnight_epoch(day_text) + wait:
        return "midnight_window_open"
    recovery = state["recoveries"].get(day_text) or {}
    if int(recovery.get("attempts") or 0) >= max_attempts:
        return "attempt_limit"
    last = float(recovery.get("last_attempt_at") or 0)
    if t < float(recovery.get("next_allowed_at") or 0) or (last and t - last < cooldown):
        return "cooldown"
    return None


def recovery_due(*, now: float | None = None) -> bool:
    t = float(time.time() if now is None else now)
    with state_store.file_lock(STATE_FILE, operation="xaus-midnight-baseline"):
        return _recovery_reason(_load_locked(), t) is None


def _retry_after(exc: Exception, now: float) -> int | None:
    if not isinstance(exc, urllib.error.HTTPError) or int(exc.code) != 429:
        return None
    raw = (exc.headers or {}).get("Retry-After", "")
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        try:
            seconds = math.ceil(parsedate_to_datetime(raw).timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return None
    return max(60, min(seconds, 86400))


def parse_intraday(data: dict, *, now: float, target_day: str,
                   tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS) -> dict:
    """Validate actual sampler metadata, sort/dedupe, never select prior day.

    A warm partial series is allowed and explicitly reported as such. Timestamp
    conflicts/future points invalidate the set; invalid prices are excluded.
    Actual intraday omits updated_at: validate as_of/latest directly instead.
    """
    if (str(data.get("symbol") or "").lower() != "xau"
            or str(data.get("currency") or "").upper() != "USD"
            or str(data.get("unit") or "").lower() != "troy_oz"):
        raise ValueError("intraday instrument/currency/unit is invalid")
    source = str(data.get("source") or "").lower()
    interval = _strict_int(data.get("interval_seconds"))
    if (not source.startswith("xaus-sampler") or "gc=f" in source
            or _strict_int(data.get("hours")) != 48
            or interval is None or not 30 <= interval <= 3600):
        raise ValueError("intraday source/horizon/interval is invalid")
    state = data.get("data_state") if isinstance(data.get("data_state"), dict) else {}
    if state.get("status") != "fresh" or str(state.get("source") or "").lower() != "sampler":
        raise ValueError("intraday data_state is not fresh sampler data")
    points = data.get("points")
    if not isinstance(points, list) or not 2 <= len(points) <= 6000 or _strict_int(data.get("count")) != len(points):
        raise ValueError("intraday points/count mismatch")
    prices: dict[int, float] = {}
    timestamps = set()
    invalid = 0
    for point in points:
        if not isinstance(point, dict):
            raise ValueError("intraday point is not an object")
        stamp = _strict_int(point.get("t"))
        if stamp is None or stamp > int(now) or stamp <= 0:
            raise ValueError("intraday invalid/future timestamp")
        timestamps.add(stamp)
        price = _finite_positive(point.get("p"))
        if price is None:
            invalid += 1
            continue
        if stamp in prices and prices[stamp] != price:
            raise ValueError("intraday duplicate timestamp has conflicting prices")
        prices[stamp] = price
    ordered = sorted(timestamps)
    if len(prices) < 2:
        raise ValueError("intraday needs at least two valid historical points")
    coverage = ordered[-1] - ordered[0]
    if (_strict_int(data.get("coverage_seconds")) != coverage or coverage < interval
            or coverage > 48 * 3600 + interval * 2
            or ordered[0] < int(now) - 48 * 3600 - interval * 2):
        raise ValueError("intraday coverage invalid")
    as_of = _iso_epoch(state.get("as_of"))
    freshness = max(300, interval * 2)
    if (as_of is None or as_of > int(now) or now - as_of > freshness
            or now - ordered[-1] > freshness or abs(as_of - ordered[-1]) > interval * 2):
        raise ValueError("intraday stale/future/mismatched source time")
    if "updated_at" in data:
        updated = _iso_epoch(data["updated_at"])
        if updated is None or updated > int(now) or now - updated > 300:
            raise ValueError("intraday aged/future response")
    age = state.get("age_seconds")
    if age is not None and (isinstance(age, bool) or not isinstance(age, (int, float))
                            or not math.isfinite(age) or age < 0 or age > freshness):
        raise ValueError("intraday invalid provider age")
    midnight = _midnight_epoch(target_day)
    if target_day != _day_text(now):
        raise ValueError("intraday target is not current Beijing day")
    valid = [(stamp, price) for stamp, price in prices.items()
             if midnight <= stamp < midnight + 86400]
    if not valid:
        raise LookupError("no valid same-day intraday point")
    stamp, price = min(valid)
    row = _record(price, stamp, target_day, source, "intraday_earliest_available", now)
    row["history_quality"] = {"count": len(points), "valid_count": len(prices),
        "invalid_price_count": invalid, "coverage_seconds": coverage,
        "first_available_at": ordered[0], "last_available_at": ordered[-1],
        "covers_midnight": ordered[0] <= midnight <= ordered[-1],
        "maximum_gap_seconds": max(b - a for a, b in zip(ordered, ordered[1:])),
        "source_as_of": as_of, "response_updated_at": _iso_epoch(data.get("updated_at"))}
    return row


def maybe_recover_usd(*, now: float | None = None,
                      requester: Callable[..., tuple[int, Optional[dict]]] | None = None,
                      tolerance_seconds: int | None = None) -> dict:
    """Called by gold_feed under the common provider lock and 60s budget."""
    t = float(time.time() if now is None else now)
    day_text = _day_text(t)
    with state_store.file_lock(STATE_FILE, operation="xaus-midnight-baseline"):
        state = _load_locked()
        reason = _recovery_reason(state, t)
        if reason:
            return {"attempted": False, "reason": reason}
        recovery = state["recoveries"].get(day_text) or {}
        state["recoveries"][day_text] = {"attempts": int(recovery.get("attempts") or 0) + 1,
            "last_attempt_at": int(t), "status": "in_progress"}
        _save_locked(state)
    selected = None
    retry_after = None
    try:
        status_code, data = (requester or _request_intraday)()
        if status_code != 200 or not isinstance(data, dict):
            raise urllib.error.HTTPError(INTRADAY_ENDPOINT, status_code, "unexpected status", {}, None)
        selected = parse_intraday(data, now=t, target_day=day_text)
        status_text = "recovered"
    except LookupError:
        status_text = "no_same_day_point"
    except Exception as exc:  # noqa: BLE001
        retry_after = _retry_after(exc, t)
        status_text = ("rate_limited" if isinstance(exc, urllib.error.HTTPError) and exc.code == 429
                       else "network" if isinstance(exc, (TimeoutError, urllib.error.URLError, OSError))
                       else "invalid_response")
    with state_store.file_lock(STATE_FILE, operation="xaus-midnight-baseline"):
        state = _load_locked()
        if state.get("last_state_error"):
            return {"attempted": True, "status": "state_unavailable", "baseline": None}
        if selected is not None and not _valid_record(state["records"].get(day_text), day_text, now=t):
            state["records"][day_text] = selected
        recovery = state["recoveries"].get(day_text) or {}
        recovery.update({"last_attempt_at": int(t), "status": status_text,
                         "next_allowed_at": t + (retry_after or 0)})
        state["recoveries"][day_text] = recovery
        _save_locked(state)
    return {"attempted": True, "status": status_text, "baseline": selected, "retry_after_s": retry_after}


def attach_changes(item: dict, *, now: float | None = None) -> dict:
    result = copy.deepcopy(item)
    t = float(time.time() if now is None else now)
    day_text = _day_text(t)
    with state_store.file_lock(STATE_FILE, operation="xaus-midnight-baseline"):
        state = _load_locked()
        row = copy.deepcopy(state["records"].get(day_text))
    if not _valid_record(row, day_text, now=t) or state.get("last_state_error"):
        row = None
    stamp = _epoch(result.get("price_as_of"))
    same_day = stamp is not None and stamp <= t and _day_text(stamp) == day_text
    p = _finite_positive(result.get("spot_usd_oz"))
    b = row["usd"]["value"] if row else None
    usd_delta = p - b if (same_day and p is not None and b is not None
                         and result.get("calculation_schema") == SCHEMA) else None
    # A retained CNY snapshot is a whole prior response, never new P + old FX.
    cny_stamp = _epoch(result.get("cny_price_as_of"))
    cny_p = _finite_positive(result.get("cny_spot_usd_oz"))
    cny_r = _finite_positive(result.get("cny_fx_rate"))
    cny_same_day = cny_stamp is not None and cny_stamp <= t and _day_text(cny_stamp) == day_text
    cny_delta = ((cny_p - b) * cny_r / GRAMS_PER_TROY_OUNCE
                 if cny_same_day and b is not None and cny_p is not None and cny_r is not None
                 and result.get("calculation_schema") == SCHEMA else None)
    kind = row["reference_kind"] if row else None
    result.update({"baseline_schema": SCHEMA, "baseline_date": day_text,
        "baseline_timezone": "Asia/Shanghai", "baseline_method": row["method"] if row else None,
        "baseline_price_as_of": row["price_as_of"] if row else None, "baseline_usd_oz": b,
        "baseline_reference_kind": kind, "change_usd_oz_since_reference": usd_delta,
        "change_cny_g_usd_reference": cny_delta,
        # Never let old firmware label the new CNY calculation as first quote.
        "baseline_cny_g": None, "baseline_cny_kind": None, "baseline_cny_price_as_of": None,
        "change_cny_g_since_reference": None, "change_cny_g_since_bj_midnight": None,
        "change_usd_oz_since_bj_midnight": usd_delta if kind == "midnight" else None,
        "change_status": {"date": day_text, "same_day_quote": same_day,
            "usd": "valid" if usd_delta is not None else "missing",
            "cny": "valid" if cny_delta is not None else "missing",
            "reference_kind": kind, "cny_snapshot_same_day": cny_same_day}})
    return result


def status(*, now: float | None = None) -> dict:
    t = float(time.time() if now is None else now)
    day_text = _day_text(t)
    with state_store.file_lock(STATE_FILE, operation="xaus-midnight-baseline"):
        state = _load_locked()
    row = state["records"].get(day_text)
    return {"schema": SCHEMA, "date": day_text, "timezone": "Asia/Shanghai",
            "state_error": state.get("last_state_error"),
            "baseline": copy.deepcopy(row) if _valid_record(row, day_text, now=t) else None,
            "recovery": copy.deepcopy(state["recoveries"].get(day_text))}
