"""XAUS daily references with separate USD and CNY semantics.

USD/troy-oz keeps the Beijing-midnight reference.  CNY/g *always* freezes the
first valid provider quote whose trusted quote timestamp belongs to the current
Beijing day, including an exact 00:00 quote.  It is never called a close or a
midnight reference.  Receipt time is only an observation time and is never
substituted for a missing market timestamp.
"""
from __future__ import annotations

import copy
import json
import math
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from . import state_store

PROVIDER = "xaus.com"
INSTRUMENT = "XAU"
INTRADAY_ENDPOINT = "https://xaus.com/api/v1/intraday?symbol=xau&hours=48"
STATE_FILE = state_store.state_path("xaus_gold_baselines.json")
DEFAULT_TOLERANCE_SECONDS = 120
DEFAULT_RECOVERY_COOLDOWN_SECONDS = 21600
DEFAULT_RECOVERY_MAX_ATTEMPTS = 2
MAX_PROVIDER_CLOCK_SKEW = 300
MAX_DAYS = 45
_BJ = timezone(timedelta(hours=8))


def configure_state_file(path) -> None:
    global STATE_FILE
    STATE_FILE = Path(path)


def _default_state() -> dict:
    return {"schema": 1, "timezone": "Asia/Shanghai", "records": {}, "recoveries": {}}


def _load_locked() -> dict:
    value, error = state_store._decode(STATE_FILE)  # caller owns STATE_FILE lock
    if not error and isinstance(value, dict) and value.get("schema") == 1:
        result = _default_state()
        result.update(value)
        result["records"] = dict(value.get("records") or {})
        result["recoveries"] = dict(value.get("recoveries") or {})
        return result
    result = _default_state()
    if error and error != "missing":
        result["last_state_error"] = error
    return result


def _save_locked(value: dict) -> None:
    clean = copy.deepcopy(value)
    clean.pop("last_state_error", None)
    for key in ("records", "recoveries"):
        rows = clean.get(key) or {}
        clean[key] = dict(sorted(rows.items())[-MAX_DAYS:])
    state_store._write_locked(STATE_FILE, clean, meta=True, keep_backup=True)


def _finite_positive(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
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
    except ValueError:
        return None
    return int(parsed.timestamp()) if parsed.tzinfo is not None else None


def _day_text(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, _BJ).date().isoformat()


def _midnight_epoch(day_text: str) -> int:
    day = date.fromisoformat(day_text)
    return int(datetime(day.year, day.month, day.day, tzinfo=_BJ).timestamp())


def _nearest_midnight(epoch: int, tolerance: int) -> tuple[str, int, int] | None:
    local_day = datetime.fromtimestamp(epoch, _BJ).date()
    options = []
    for offset in (-1, 0, 1):
        day = local_day + timedelta(days=offset)
        text = day.isoformat()
        midnight = _midnight_epoch(text)
        options.append((abs(epoch - midnight), epoch > midnight, epoch, text, midnight))
    distance, _, _, day_text, midnight = min(options)
    if distance > tolerance:
        return None
    return day_text, midnight, distance


def _config() -> tuple[int, int, int]:
    try:
        from .operator_config import load_effective
        gold = load_effective().config.get("gold_refresh") or {}
        tolerance = int(gold.get("midnight_tolerance_seconds", DEFAULT_TOLERANCE_SECONDS))
        cooldown = int(gold.get("intraday_recovery_cooldown_seconds",
                                DEFAULT_RECOVERY_COOLDOWN_SECONDS))
        attempts = int(gold.get("intraday_recovery_max_attempts_per_day",
                                DEFAULT_RECOVERY_MAX_ATTEMPTS))
        return (max(0, min(tolerance, 600)), max(1800, min(cooldown, 86400)),
                max(1, min(attempts, 8)))
    except Exception:  # noqa: BLE001
        return (DEFAULT_TOLERANCE_SECONDS, DEFAULT_RECOVERY_COOLDOWN_SECONDS,
                DEFAULT_RECOVERY_MAX_ATTEMPTS)


def _valid_spot(item: dict) -> bool:
    state = item.get("data_state") if isinstance(item.get("data_state"), dict) else {}
    return (item.get("provider") == PROVIDER
            and item.get("instrument_id") == INSTRUMENT
            and item.get("currency") == "CNY" and item.get("unit") == "g"
            and _finite_positive(item.get("spot_usd_oz")) is not None
            and _finite_positive(item.get("price_gram_cny")) is not None
            and _finite_positive(item.get("fx_rate")) is not None
            and _epoch(item.get("price_as_of")) is not None
            and item.get("status") == "ok" and not bool(item.get("stale"))
            and not bool(item.get("fx_stale")) and state.get("status") == "fresh")


def _valid_record(row, day_text: str) -> bool:
    if not isinstance(row, dict):
        return False
    if (row.get("date") != day_text or row.get("timezone") != "Asia/Shanghai"
            or row.get("provider") != PROVIDER or row.get("instrument_id") != INSTRUMENT
            or row.get("method") not in {"spot_cny_gram", "intraday_usd_recovery",
                                         "first_valid_cny_gram", "combined_references"}):
        return False
    usd = row.get("usd")
    midnight = _midnight_epoch(day_text)
    if usd is not None and (not isinstance(usd, dict) or usd.get("currency") != "USD"
                            or usd.get("unit") != "troy_oz"
                            or _finite_positive(usd.get("value")) is None
                            or _epoch(usd.get("price_as_of")) is None
                            or abs(int(usd.get("price_as_of")) - midnight) > 600):
        return False
    cny = row.get("cny")
    if cny is not None:
        if (not isinstance(cny, dict)
                or cny.get("currency") != "CNY" or cny.get("unit") != "g"
                or _finite_positive(cny.get("value")) is None
                or _epoch(cny.get("price_as_of")) is None
                or _cny_kind(row, cny) not in {"midnight", "first_valid"}):
            return False
        kind = _cny_kind(row, cny)
        if ((kind == "midnight" and abs(int(cny["price_as_of"]) - midnight) > 600)
                or (kind == "first_valid" and _day_text(int(cny["price_as_of"])) != day_text)):
            return False
    return usd is not None or cny is not None


def _cny_kind(row: dict, cny: dict | None = None) -> str | None:
    cny = cny if isinstance(cny, dict) else row.get("cny")
    if not isinstance(cny, dict):
        return None
    explicit = cny.get("reference_kind")
    if explicit in {"midnight", "first_valid"}:
        return explicit
    return "midnight" if row.get("method") == "spot_cny_gram" else None


def _valid_usd_reference(row, day_text: str) -> bool:
    return _valid_record(row, day_text) and isinstance(row.get("usd"), dict)


def _valid_cny_reference(row, day_text: str) -> bool:
    return (_valid_record(row, day_text) and isinstance(row.get("cny"), dict)
            and _cny_kind(row, row.get("cny")) == "first_valid"
            and _day_text(int(row["cny"]["price_as_of"])) == day_text)


def _candidate(item: dict, day_text: str, midnight: int, distance: int) -> dict:
    cny = None
    if _day_text(int(item["price_as_of"])) == day_text:
        cny = {"value": float(item["price_gram_cny"]), "currency": "CNY",
               "unit": "g", "provider": PROVIDER, "source": item.get("price_source"),
               "price_as_of": int(item["price_as_of"]),
               "reference_kind": "first_valid", "captured_at": int(item["price_as_of"]),
               "confidence": "provider_fresh", "trust": "validated_upstream"}
    return {
        "date": day_text, "timezone": "Asia/Shanghai", "midnight_at": midnight,
        "distance_seconds": distance, "provider": PROVIDER, "instrument_id": INSTRUMENT,
        "method": "spot_cny_gram", "price_as_of": int(item["price_as_of"]),
        "usd": {"value": float(item["spot_usd_oz"]), "currency": "USD",
                "unit": "troy_oz", "source": item.get("price_source"),
                "price_as_of": int(item["price_as_of"])},
        "cny": cny,
        "fx": {"value": float(item["fx_rate"]), "source": item.get("fx_source"),
               "as_of": item.get("fx_as_of"), "stale": False},
    }


def _better(left: dict, right: dict) -> bool:
    """True when left wins: nearest to midnight, tie goes to earlier point."""
    return (int(left["distance_seconds"]), int(left["price_as_of"])) < (
        int(right["distance_seconds"]), int(right["price_as_of"]))


def _freeze_due(state: dict, now: float, tolerance: int) -> bool:
    changed = False
    for day_text, row in list((state.get("records") or {}).items()):
        if not isinstance(row, dict) or row.get("frozen") or not isinstance(row.get("candidate"), dict):
            continue
        candidate = row["candidate"]
        if int(candidate.get("distance_seconds", tolerance + 1)) == 0 or now >= (
                _midnight_epoch(day_text) + tolerance):
            candidate = copy.deepcopy(candidate)
            candidate["frozen"] = True
            candidate["complete"] = True
            candidate["frozen_at"] = int(now)
            state["records"][day_text] = candidate
            changed = True
    return changed


def consider_spot(item: dict, *, now: float | None = None,
                  tolerance_seconds: int | None = None) -> bool:
    """Observe one parsed spot item; return true only when persistent state changed."""
    t = float(time.time() if now is None else now)
    configured, _, _ = _config()
    tolerance = configured if tolerance_seconds is None else int(tolerance_seconds)
    with state_store.file_lock(STATE_FILE, operation="xaus-midnight-baseline"):
        state = _load_locked()
        if state.get("last_state_error"):
            return False
        changed = _freeze_due(state, t, tolerance)
        if _valid_spot(item):
            stamp = int(item["price_as_of"])
            if stamp <= int(t) + MAX_PROVIDER_CLOCK_SKEW:
                today = _day_text(t)

                # CNY is independent of the USD midnight window.  Freeze the
                # first same-day trusted quote immediately and never replace it.
                if _day_text(stamp) == today:
                    existing = state["records"].get(today)
                    wrapper = isinstance(existing, dict) and isinstance(existing.get("candidate"), dict)
                    base = copy.deepcopy(existing.get("candidate") if wrapper else existing)
                    if not _valid_record(base, today):
                        base = {"date": today, "timezone": "Asia/Shanghai",
                                "midnight_at": _midnight_epoch(today),
                                "distance_seconds": None, "provider": PROVIDER,
                                "instrument_id": INSTRUMENT,
                                "method": "first_valid_cny_gram",
                                "price_as_of": stamp, "usd": None, "cny": None,
                                "fx": None, "frozen": True, "complete": False,
                                "frozen_at": int(t)}
                    if not _valid_cny_reference(base, today):
                        base["cny"] = {
                            "value": float(item["price_gram_cny"]), "currency": "CNY",
                            "unit": "g", "provider": PROVIDER,
                            "source": item.get("price_source"), "price_as_of": stamp,
                            "reference_kind": "first_valid", "captured_at": int(t),
                            "frozen": True, "confidence": "provider_fresh",
                            "trust": "validated_upstream"}
                        base["method"] = ("combined_references" if _valid_usd_reference(base, today)
                                          else "first_valid_cny_gram")
                        if wrapper:
                            existing = copy.deepcopy(existing)
                            existing["candidate"] = base
                            state["records"][today] = existing
                        else:
                            state["records"][today] = base
                        changed = True

                # USD alone keeps the existing nearest-midnight selection.
                nearest = _nearest_midnight(stamp, tolerance)
                if nearest is not None:
                    day_text, midnight, distance = nearest
                    existing = state["records"].get(day_text)
                    wrapper = isinstance(existing, dict) and isinstance(existing.get("candidate"), dict)
                    current = copy.deepcopy(existing.get("candidate") if wrapper else existing)
                    usd_exists = _valid_usd_reference(current, day_text)
                    can_improve = wrapper or not usd_exists
                    if can_improve:
                        cand = _candidate(item, day_text, midnight, distance)
                        if _valid_cny_reference(current, day_text):
                            cand["cny"] = copy.deepcopy(current["cny"])
                        old = existing.get("candidate") if wrapper else None
                        if not isinstance(old, dict) or _better(cand, old):
                            state["records"][day_text] = {
                                "date": day_text, "timezone": "Asia/Shanghai",
                                "candidate": cand, "frozen": False, "complete": False,
                            }
                            changed = True
                        changed = _freeze_due(state, t, tolerance) or changed
        if changed:
            _save_locked(state)
        return changed


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


def parse_intraday(data: dict, *, now: float, target_day: str,
                   tolerance_seconds: int) -> dict:
    """Validate one 48-hour XAUS sample set and select the midnight neighbor."""
    if str(data.get("symbol") or "").lower() != "xau":
        raise ValueError("intraday symbol is not xau")
    if str(data.get("currency") or "").upper() != "USD":
        raise ValueError("intraday currency is not USD")
    if str(data.get("unit") or "").lower() != "troy_oz":
        raise ValueError("intraday unit is not troy_oz")
    source = str(data.get("source") or "").lower()
    if not source.startswith("xaus-sampler") or "gc=f" in source:
        raise ValueError("intraday source is not the XAUS spot sampler")
    hours = _strict_int(data.get("hours"))
    interval = _strict_int(data.get("interval_seconds"))
    if hours != 48 or interval is None or not 30 <= interval <= 3600:
        raise ValueError("intraday horizon/interval is invalid")
    state = data.get("data_state") if isinstance(data.get("data_state"), dict) else {}
    if state.get("status") != "fresh" or str(state.get("source") or "").lower() != "sampler":
        raise ValueError("intraday data_state is not fresh sampler data")
    points = data.get("points")
    if not isinstance(points, list) or not 2 <= len(points) <= 3000:
        raise ValueError("intraday points missing")
    if _strict_int(data.get("count")) != len(points):
        raise ValueError("intraday count mismatch")
    parsed: list[tuple[int, float]] = []
    previous = None
    for point in points:
        if not isinstance(point, dict):
            raise ValueError("intraday point is not an object")
        stamp = _strict_int(point.get("t"))
        price = _finite_positive(point.get("p"))
        if stamp is None or price is None or stamp > int(now) + MAX_PROVIDER_CLOCK_SKEW:
            raise ValueError("intraday point has invalid/future time or price")
        if previous is not None and stamp <= previous:
            raise ValueError("intraday timestamps are unordered or duplicated")
        previous = stamp
        parsed.append((stamp, price))
    coverage = parsed[-1][0] - parsed[0][0]
    if _strict_int(data.get("coverage_seconds")) != coverage or coverage <= 0:
        raise ValueError("intraday coverage mismatch")
    state_as_of = _iso_epoch(state.get("as_of"))
    if state_as_of is None or state_as_of > int(now) + MAX_PROVIDER_CLOCK_SKEW:
        raise ValueError("intraday data_state time is invalid/future")
    if abs(state_as_of - parsed[-1][0]) > max(interval * 2, MAX_PROVIDER_CLOCK_SKEW):
        raise ValueError("intraday data_state time does not match series")
    midnight = _midnight_epoch(target_day)
    if not (parsed[0][0] <= midnight <= parsed[-1][0]):
        raise ValueError("intraday series does not cover target midnight")
    in_window = [(abs(stamp - midnight), stamp, price) for stamp, price in parsed
                 if abs(stamp - midnight) <= tolerance_seconds]
    if not in_window:
        raise LookupError("no intraday point within midnight tolerance")
    distance, stamp, price = min(in_window, key=lambda row: (row[0], row[1]))
    return {"date": target_day, "timezone": "Asia/Shanghai", "midnight_at": midnight,
            "distance_seconds": distance, "provider": PROVIDER,
            "instrument_id": INSTRUMENT, "method": "intraday_usd_recovery",
            "price_as_of": stamp,
            "usd": {"value": price, "currency": "USD", "unit": "troy_oz",
                    "source": source, "price_as_of": stamp},
            "cny": None, "fx": None, "frozen": True, "complete": False,
            "frozen_at": int(now)}


def maybe_recover_usd(*, now: float | None = None,
                      requester: Callable[..., tuple[int, Optional[dict]]] | None = None,
                      tolerance_seconds: int | None = None) -> dict:
    """Try one cooldown-gated USD recovery. Never invents a CNY baseline."""
    t = float(time.time() if now is None else now)
    configured_tolerance, cooldown, max_attempts = _config()
    tolerance = configured_tolerance if tolerance_seconds is None else int(tolerance_seconds)
    day_text = _day_text(t)
    if t < _midnight_epoch(day_text) + tolerance:
        return {"attempted": False, "reason": "midnight_window_open"}
    with state_store.file_lock(STATE_FILE, operation="xaus-midnight-baseline"):
        state = _load_locked()
        if state.get("last_state_error"):
            return {"attempted": False, "reason": "state_unavailable"}
        changed = _freeze_due(state, t, tolerance)
        row = state["records"].get(day_text)
        if _valid_usd_reference(row, day_text):
            if changed:
                _save_locked(state)
            return {"attempted": False, "reason": "usd_baseline_exists"}
        recovery = state["recoveries"].get(day_text) or {}
        attempts = int(recovery.get("attempts") or 0)
        last = float(recovery.get("last_attempt_at") or 0)
        if attempts >= max_attempts:
            if changed:
                _save_locked(state)
            return {"attempted": False, "reason": "attempt_limit"}
        if 0 <= t - last < cooldown:
            if changed:
                _save_locked(state)
            return {"attempted": False, "reason": "cooldown"}
        state["recoveries"][day_text] = {
            "attempts": attempts + 1, "last_attempt_at": int(t), "status": "in_progress"}
        _save_locked(state)
    request_fn = requester or _request_intraday
    status_text = "invalid_response"
    selected = None
    try:
        status, data = request_fn()
        if status != 200 or not isinstance(data, dict):
            raise urllib.error.HTTPError(INTRADAY_ENDPOINT, status, "unexpected status", {}, None)
        selected = parse_intraday(data, now=t, target_day=day_text,
                                  tolerance_seconds=tolerance)
        status_text = "recovered"
    except LookupError:
        status_text = "no_point_within_tolerance"
    except Exception as exc:  # noqa: BLE001
        status_text = "network" if isinstance(exc, (TimeoutError, urllib.error.URLError, OSError)) \
            else "invalid_response"
    with state_store.file_lock(STATE_FILE, operation="xaus-midnight-baseline"):
        state = _load_locked()
        if state.get("last_state_error"):
            return {"attempted": True, "status": "state_unavailable", "baseline": None}
        if selected is not None:
            row = state["records"].get(day_text)
            if not _valid_usd_reference(row, day_text):
                if _valid_cny_reference(row, day_text):
                    merged = copy.deepcopy(row)
                    merged.update({"method": "combined_references",
                                   "midnight_at": selected["midnight_at"],
                                   "distance_seconds": selected["distance_seconds"],
                                   "price_as_of": selected["price_as_of"],
                                   "usd": selected["usd"], "frozen": True,
                                   "complete": False, "frozen_at": int(t)})
                    state["records"][day_text] = merged
                else:
                    state["records"][day_text] = selected
        recovery = state["recoveries"].get(day_text) or {}
        recovery.update({"last_attempt_at": int(t), "status": status_text})
        state["recoveries"][day_text] = recovery
        _save_locked(state)
    return {"attempted": True, "status": status_text, "baseline": selected}


def _normal_delta(current: float, baseline: float) -> float:
    value = current - baseline
    return 0.0 if round(value, 2) == 0 else value


def attach_changes(item: dict, *, now: float | None = None) -> dict:
    """Return a copy with same-day deltas; unknown stays None, never zero-filled."""
    result = copy.deepcopy(item)
    t = float(time.time() if now is None else now)
    day_text = _day_text(t)
    tolerance, _, _ = _config()
    with state_store.file_lock(STATE_FILE, operation="xaus-midnight-baseline"):
        state = _load_locked()
        if not state.get("last_state_error") and _freeze_due(state, t, tolerance):
            _save_locked(state)
        row = copy.deepcopy(state["records"].get(day_text))
    if (isinstance(row, dict) and isinstance(row.get("candidate"), dict)
            and _valid_record(row.get("candidate"), day_text)):
        row = row["candidate"]
    if not _valid_record(row, day_text):
        row = None
    quote_at = _epoch(result.get("price_as_of"))
    same_day_quote = quote_at is not None and _day_text(quote_at) == day_text
    usd = row.get("usd") if isinstance(row, dict) and isinstance(row.get("usd"), dict) else None
    cny = (row.get("cny") if isinstance(row, dict) and _valid_cny_reference(row, day_text)
           else None)
    cny_kind = _cny_kind(row, cny) if isinstance(row, dict) else None
    result["baseline_date"] = day_text
    result["baseline_timezone"] = "Asia/Shanghai"
    result["baseline_tolerance_seconds"] = tolerance
    result["baseline_method"] = row.get("method") if isinstance(row, dict) else None
    result["baseline_price_as_of"] = row.get("price_as_of") if isinstance(row, dict) else None
    result["baseline_usd_oz"] = usd.get("value") if usd else None
    result["baseline_cny_g"] = cny.get("value") if cny else None
    result["baseline_cny_kind"] = cny_kind
    result["baseline_cny_price_as_of"] = cny.get("price_as_of") if cny else None
    result["change_usd_oz_since_bj_midnight"] = (
        _normal_delta(float(result["spot_usd_oz"]), float(usd["value"]))
        if same_day_quote and usd and _finite_positive(result.get("spot_usd_oz")) is not None
        else None)
    cny_delta = (
        _normal_delta(float(result["price_gram_cny"]), float(cny["value"]))
        if same_day_quote and cny and _finite_positive(result.get("price_gram_cny")) is not None
        else None)
    result["change_cny_g_since_reference"] = cny_delta
    result["change_cny_g_since_bj_midnight"] = (
        cny_delta if cny_kind == "midnight" else None)
    result["change_status"] = {
        "date": day_text, "same_day_quote": same_day_quote,
        "usd": "valid" if result["change_usd_oz_since_bj_midnight"] is not None else "missing",
        "cny": "valid" if cny_delta is not None else "missing",
        "cny_reference_kind": cny_kind,
    }
    return result


def status(*, now: float | None = None) -> dict:
    t = float(time.time() if now is None else now)
    day_text = _day_text(t)
    value, error = state_store.read_json(STATE_FILE)
    records = value.get("records") if isinstance(value, dict) else {}
    recoveries = value.get("recoveries") if isinstance(value, dict) else {}
    return {"date": day_text, "timezone": "Asia/Shanghai", "state_error": error,
            "baseline": copy.deepcopy((records or {}).get(day_text)),
            "recovery": copy.deepcopy((recoveries or {}).get(day_text))}
