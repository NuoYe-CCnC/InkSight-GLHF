"""Credential-free XAUS spot feed for the InkSight gold panel.

The XAUS namespace is intentionally separate from the legacy GoldAPI cache, so
an old quote can never masquerade as XAUS. Provider source time, XAUS response
time, and this host's receipt time remain distinct. XAUS does not publish an FX
as-of timestamp, so ``fx_as_of`` is explicitly unknown.
"""
from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import data_cache as dc
from . import gold_baseline
from . import state_store

logger = logging.getLogger(__name__)

GROUP = "gold.xaus.usd_fx.v2"
ENDPOINT = "https://xaus.com/api/v1/spot?currency=CNY&unit=gram&compact=1"
PROVIDER = "xaus.com"
INSTRUMENT = {"provider": PROVIDER, "instrument": "XAU", "instrument_id": "XAU",
              "instrument_label": "伦敦金参考价", "currency": "CNY", "unit": "g"}
PENDING_NOTE = None
STATE_FILE = state_store.state_path("xaus_gold_state.json")
MIN_REQUEST_INTERVAL_S = 60
MAX_PROVIDER_CLOCK_SKEW = 300
DEFAULT_RESPONSE_MAX_AGE_S = 300
DEFAULT_QUOTE_MAX_AGE_S = 7200
DEFAULT_CACHE_MAX_AGE_S = 2700
MAX_RETRY_ATTEMPTS = 3
_TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}
_BJ = timezone(timedelta(hours=8))


def configure_state_file(path) -> None:
    global STATE_FILE
    STATE_FILE = Path(path)


def _bj_now() -> datetime:
    return datetime.now(_BJ)


def _key() -> str:
    """Legacy compatibility hook. XAUS itself needs no credential."""
    return "xaus-public"


def _default_state() -> dict:
    return {"schema": 1, "last_request_at": 0.0, "scheduled_slots": {},
            "logical_requests": {}, "pending_requests": {},
            "last_success_at": 0.0, "next_allowed_at": 0.0,
            "stats": {"requests": 0, "retries": 0, "success": 0,
                      "failure": 0, "coalesced": 0, "deduped": 0}}


def _load_state() -> dict:
    value, error = state_store.read_json(STATE_FILE)
    if isinstance(value, dict) and value.get("schema") == 1 and not error:
        base = _default_state()
        base.update(value)
        base["stats"] = {**_default_state()["stats"], **(value.get("stats") or {})}
        base.setdefault("scheduled_slots", {})
        base.setdefault("logical_requests", {})
        base.setdefault("pending_requests", {})
        return base
    if error and error != "missing":
        logger.warning("[GOLD] XAUS state unavailable: %s", error)
    base = _default_state()
    if error:
        base["_state_error"] = error
    return base


def _save_state(value: dict) -> None:
    clean = dict(value)
    clean.pop("_state_error", None)
    for key, keep in (("scheduled_slots", 96), ("logical_requests", 256),
                      ("pending_requests", 96)):
        rows = clean.get(key) or {}
        if len(rows) > keep:
            ordered = sorted(rows.items(), key=lambda pair: float((pair[1] or {}).get("at", 0)))
            clean[key] = dict(ordered[-keep:])
    state_store.write_json(STATE_FILE, clean)


def _fnum(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _iso_epoch(value) -> Optional[int]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return int(dt.timestamp()) if dt.tzinfo is not None else None
    except ValueError:
        return None


def _freshness_limits() -> tuple[int, int, int]:
    """Response generation, market quote and host-cache ages are independent."""
    try:
        from .operator_config import load_effective
        config = load_effective().config.get("gold_refresh") or {}
    except Exception:  # noqa: BLE001
        config = {}
    values = []
    for key, default in (("response_max_age_seconds", DEFAULT_RESPONSE_MAX_AGE_S),
                         ("quote_max_age_seconds", DEFAULT_QUOTE_MAX_AGE_S),
                         ("cache_max_age_seconds", DEFAULT_CACHE_MAX_AGE_S)):
        try:
            value = int(config.get(key, default))
        except (TypeError, ValueError):
            value = default
        values.append(max(60, min(value, 86400)))
    return tuple(values)


def _parse(data: dict, fetched_at: float) -> dict:
    xau = data.get("xau")
    if not isinstance(xau, dict):
        raise ValueError("missing xau object")
    currency = str(xau.get("currency") or "").upper()
    unit = str(xau.get("unit") or "").lower()
    reported_cny_g = _fnum(xau.get("price"))  # diagnostics, never calculation authority
    usd_oz = _fnum(data.get("spot_usd_oz"))
    fx = _fnum(data.get("fx_rate"))
    if currency != "CNY" or unit not in {"gram", "g"}:
        raise ValueError(f"unexpected xau unit {currency}/{unit}")
    if usd_oz is None or usd_oz <= 0:
        raise ValueError("non-positive or non-finite USD price")
    source = str(data.get("source") or "").lower()
    if source and source != PROVIDER:
        raise ValueError(f"unexpected response source {source}")
    updated_at = _iso_epoch(data.get("updated_at"))
    price_as_of = _iso_epoch(data.get("price_as_of"))
    state = data.get("data_state") if isinstance(data.get("data_state"), dict) else {}
    state_as_of = _iso_epoch(state.get("as_of"))
    if price_as_of is None:
        price_as_of = state_as_of
    now_i = int(fetched_at)
    for label, stamp in (("updated_at", updated_at), ("price_as_of", price_as_of)):
        if stamp is None:
            raise ValueError(f"missing/invalid {label}")
        if stamp > now_i:
            raise ValueError(f"future {label}")
    if state_as_of is not None and state_as_of > now_i:
        raise ValueError("future data_state.as_of")
    provider_stale = bool(data.get("stale"))
    state_status = str(state.get("status") or ("stale" if provider_stale else "fresh")).lower()
    if state_status not in {"fresh", "stale", "degraded", "unknown", "error"}:
        state_status = "unknown"
    response_max_age, quote_max_age, _ = _freshness_limits()
    if now_i - updated_at > response_max_age:
        freshness_reason = "response_aged"
    elif now_i - price_as_of > quote_max_age:
        freshness_reason = "quote_aged"
    elif provider_stale or state_status != "fresh":
        freshness_reason = "provider_stale"
    else:
        freshness_reason = None
    usd_status = "stale" if freshness_reason else "fresh"
    fx_invalid = fx is None or fx <= 0
    fx_stale = bool(data.get("fx_stale")) or fx_invalid
    cny_reason = freshness_reason or ("fx_invalid" if fx_invalid else "fx_stale" if fx_stale else None)
    cny_g = (usd_oz * fx / gold_baseline.GRAMS_PER_TROY_OUNCE
             if cny_reason is None else None)
    if cny_g is not None and not math.isfinite(cny_g):
        cny_g, cny_reason = None, "conversion_nonfinite"
    freshness_reason = freshness_reason or cny_reason
    # XAUS spot JSON has no documented market-open state. A stale price does
    # not prove a normal market closure, even during a weekend.
    market_state = ("closed" if data.get("market_status") == "closed"
                    and state.get("source") == "upstream" else "unknown")
    return {
        "provider": PROVIDER, "instrument_id": "XAU",
        "instrument_label": "伦敦金参考价",
        "calculation_schema": gold_baseline.SCHEMA,
        "calculation_method": "usd_fx_same_response",
        "grams_per_troy_ounce": gold_baseline.GRAMS_PER_TROY_OUNCE,
        "spot_usd_oz": usd_oz, "price_gram_cny": cny_g,
        "reported_price_gram_cny": reported_cny_g,
        "currency": "CNY", "unit": "g", "fx_rate": fx,
        "fx_rate_display": _rounded_optional(fx),
        "usd_status": usd_status,
        "cny_status": "fresh" if cny_reason is None else "unavailable",
        "cny_freshness_reason": cny_reason,
        "cny_spot_usd_oz": usd_oz if cny_g is not None else None,
        "cny_fx_rate": fx if cny_g is not None else None,
        "cny_price_as_of": price_as_of if cny_g is not None else None,
        "cny_updated_at": updated_at if cny_g is not None else None,
        "cny_fetched_at": now_i if cny_g is not None else None,
        "price_source": str(data.get("price_source") or "unknown"),
        "price_as_of": price_as_of,
        "quote_time": price_as_of,  # old firmware compatibility; same honest source time
        "data_state": {"status": state_status, "as_of": state_as_of,
                       "source": str(state.get("source") or "unknown")},
        "stale": freshness_reason is not None,
        "provider_stale": bool(data.get("stale")),
        "fx_source": str(data.get("fx_source") or "unknown"),
        "fx_stale": fx_stale, "fx_as_of": None,
        "updated_at": updated_at, "fetched_at": now_i,
        "status": "stale" if freshness_reason else "ok",
        "freshness_reason": freshness_reason, "market_state": market_state,
        "note": freshness_reason,
    }


def _request(timeout_sec: float = 12) -> tuple[int, Optional[dict]]:
    req = urllib.request.Request(ENDPOINT, headers={
        "Accept": "application/json", "User-Agent": "InkSight/3.0 (+https://xaus.com/api)"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as response:
        status = int(getattr(response, "status", 200))
        body = response.read()
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None
    return status, value if isinstance(value, dict) else None


def _visible_seed(item: dict, cache_status: str) -> dict:
    """Only visible/status changes redraw; receipt time/sub-cent jitter do not."""
    return {"calculation_schema": item.get("calculation_schema"),
            "spot_usd_oz": _rounded_optional(item.get("spot_usd_oz")),
            "price_gram_cny": _rounded_optional(item.get("price_gram_cny")),
            "fx_rate": _rounded_optional(item.get("fx_rate")),
            "price_source": item.get("price_source"),
            "price_as_of": item.get("price_as_of"),
            "baseline_date": item.get("baseline_date"),
            "baseline_method": item.get("baseline_method"),
            "baseline_price_as_of": item.get("baseline_price_as_of"),
            "baseline_reference_kind": item.get("baseline_reference_kind"),
            "cny_price_as_of": item.get("cny_price_as_of"),
            "usd_status": item.get("usd_status"), "cny_status": item.get("cny_status"),
            "change_usd_oz": _rounded_optional(
                item.get("change_usd_oz_since_reference")),
            "change_cny_g": _rounded_optional(
                item.get("change_cny_g_usd_reference")),
            "change_status": item.get("change_status"),
            "data_state": (item.get("data_state") or {}).get("status"),
            "stale": bool(item.get("stale")),
            "freshness_reason": item.get("freshness_reason"),
            "market_state": item.get("market_state"),
            "fx_source": item.get("fx_source"),
            "fx_stale": bool(item.get("fx_stale")), "cache_status": cache_status}


def visual_seed(item: dict, cache_status: str | None = None) -> dict:
    """Public redraw seed shared by scheduled and document build paths."""
    return _visible_seed(item, cache_status or effective_cache_status() or "unknown")


def _rounded_optional(value):
    number = _fnum(value)
    if number is None:
        return None
    rounded = round(number, 2)
    return 0.0 if rounded == 0 else rounded


def _attach_changes_safely(item: dict, *, now: float | None = None) -> dict:
    try:
        return gold_baseline.attach_changes(item, now=now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[GOLD] midnight baseline unavailable: %s", exc)
        result = dict(item)
        t = float(time.time() if now is None else now)
        day_text = datetime.fromtimestamp(t, _BJ).date().isoformat()
        result.update({
            "baseline_date": day_text, "baseline_timezone": "Asia/Shanghai",
            "baseline_tolerance_seconds": None, "baseline_method": None,
            "baseline_price_as_of": None, "baseline_usd_oz": None,
            "baseline_cny_g": None, "baseline_cny_kind": None,
            "baseline_cny_price_as_of": None,
            "change_usd_oz_since_bj_midnight": None,
            "change_cny_g_since_bj_midnight": None,
            "change_cny_g_since_reference": None,
            "change_usd_oz_since_reference": None,
            "change_cny_g_usd_reference": None,
            "baseline_reference_kind": None, "baseline_schema": gold_baseline.SCHEMA,
            "change_status": {"date": day_text, "same_day_quote": False,
                              "usd": "missing", "cny": "missing",
                              "cny_reference_kind": None},
        })
        return result


def _classify_error(exc) -> tuple[str, bool]:
    if isinstance(exc, urllib.error.HTTPError):
        code = int(exc.code)
        if code == 429:
            return "rate_limited", False
        return f"http_{code}", code in _TRANSIENT_HTTP
    if isinstance(exc, (TimeoutError, urllib.error.URLError, OSError)):
        return "network", True
    return "invalid_response", False


def _fetch_once(fetched_at: float) -> dict:
    started = time.monotonic()
    status, data = _request()
    if status != 200:
        raise urllib.error.HTTPError(ENDPOINT, status, "unexpected status", {}, None)
    if not data:
        raise ValueError("empty/non-object JSON")
    return _parse(data, fetched_at + max(0, time.monotonic() - started))


def _scheduled_slot(now: datetime | None = None) -> str:
    current = now or _bj_now()
    minute = 0 if current.minute < 30 else 30
    return current.replace(minute=minute, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M%z")


def _mode_enabled(at: datetime | None = None) -> bool:
    try:
        from .operator_config import load_effective
        from .news_calendar import is_workday
        config = load_effective().config
        gold = config.get("gold_refresh") or {}
        if not gold.get("scheduled_enabled", True):
            return False
        now = at or _bj_now()
        day_type = "workday" if is_workday(now) else "restday"
        minute = now.hour * 60 + now.minute
        mode = "active"
        for row in ((config.get("device_policy") or {}).get("activity_windows") or {}).get(day_type, []):
            start_h, start_m = (int(part) for part in row["start"].split(":"))
            end_h, end_m = (int(part) for part in row["end"].split(":"))
            start, end = start_h * 60 + start_m, end_h * 60 + end_m
            inside = start <= minute < end if start < end else (minute >= start or minute < end)
            if inside:
                mode = str(row.get("mode") or mode)
                break
        return bool((gold.get("mode_enabled") or {}).get(mode, True))
    except Exception:  # noqa: BLE001
        return True


def _minimum_interval() -> int:
    try:
        from .operator_config import load_effective
        value = int((load_effective().config.get("gold_refresh") or {}).get(
            "minimum_request_interval_seconds", MIN_REQUEST_INTERVAL_S))
        return max(MIN_REQUEST_INTERVAL_S, min(value, 3600))
    except Exception:  # noqa: BLE001
        return MIN_REQUEST_INTERVAL_S


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
    return max(MIN_REQUEST_INTERVAL_S, min(seconds, 86400))


def _pending_result(state: dict, key: str, reason: str, slot: str | None,
                    now: float, next_at: float, attempts: int,
                    error: str, *, attempted: bool = False,
                    item: dict | None = None) -> dict:
    terminal = attempts >= MAX_RETRY_ATTEMPTS
    state.setdefault("pending_requests", {}).pop(key, None)
    if terminal:
        state.setdefault("logical_requests", {})[key] = {
            "at": now, "ok": False, "result": error, "terminal": True}
    else:
        state["pending_requests"][key] = {
            "at": now, "reason": reason, "slot": slot, "attempts": attempts,
            "next_at": next_at, "last_error": error}
    _save_state(state)
    return {"ok": False, "pending": not terminal, "terminal": terminal,
            "attempted": attempted, "request_id": key,
            "retry_after_s": max(1, math.ceil(next_at - now)) if not terminal else None,
            "error_class": error, "status": effective_cache_status(now=now),
            "item": item if item is not None else cached(now=now)}


def request_state(request_id: str) -> str:
    state = _load_state()
    if request_id in (state.get("pending_requests") or {}):
        return "pending"
    prior = (state.get("logical_requests") or {}).get(request_id)
    if isinstance(prior, dict):
        return "complete" if prior.get("ok") else "terminal"
    return "new"


def refresh(force: bool = False, *, reason: str = "scheduled",
            request_id: str | None = None, now: float | None = None) -> dict:
    """One bounded XAUS request, shared by cron, host recovery and device wake."""
    t = float(now if now is not None else time.time())
    logical_id = str(request_id or "")[:96]
    with state_store.file_lock(STATE_FILE, operation="xaus-gold-refresh"):
        state = _load_state()
        if state.get("_state_error") not in (None, "missing"):
            return {"ok": False, "error_class": "state_unavailable",
                    "status": effective_cache_status(now=t), "item": cached(now=t)}
        if reason == "scheduled" and not force:
            if not _mode_enabled(datetime.fromtimestamp(t, _BJ)):
                return {"ok": False, "disabled": True, "status": dc.group_status(GROUP),
                        "item": cached(now=t)}
            slot = _scheduled_slot(datetime.fromtimestamp(t, _BJ))
            if (state.get("scheduled_slots") or {}).get(slot, {}).get("status") == "done":
                return {"ok": False, "window_done": True, "window": slot,
                        "status": dc.group_status(GROUP), "item": cached(now=t)}
        else:
            slot = None
        if not logical_id:
            logical_id = f"slot:{slot}" if slot else f"event:{reason}:{int(t // 60)}"
        queued = (state.get("pending_requests") or {}).get(logical_id) or {}
        if queued.get("slot"):
            slot = queued["slot"]
        prior = (state.get("logical_requests") or {}).get(logical_id) if logical_id else None
        if isinstance(prior, dict):
            state["stats"]["deduped"] += 1
            _save_state(state)
            return {"ok": bool(prior.get("ok")), "deduped": True,
                    "terminal": True,
                    "request_id": logical_id, "status": dc.group_status(GROUP),
                    "item": cached(now=t)}
        age = t - float(state.get("last_request_at") or 0)
        minimum_interval = _minimum_interval()
        allowed_at = max(float(state.get("last_request_at") or 0) + minimum_interval,
                         float(state.get("next_allowed_at") or 0),
                         float(queued.get("next_at") or 0))
        if t < allowed_at:
            state["stats"]["coalesced"] += 1
            last_success = float(state.get("last_success_at") or 0)
            # A genuinely successful concurrent fetch may satisfy the event.
            if (0 <= age < minimum_interval and last_success >=
                    float(state.get("last_request_at") or 0) and
                    cached(now=t) and effective_cache_status(now=t) == "fresh"):
                state["logical_requests"][logical_id] = {
                    "at": t, "ok": True, "result": "coalesced"}
                if slot:
                    state["scheduled_slots"][slot] = {"at": t, "status": "done"}
                state["pending_requests"].pop(logical_id, None)
                _save_state(state)
                return {"ok": True, "coalesced": True, "terminal": True,
                        "request_id": logical_id, "status": "fresh", "item": cached(now=t)}
            pending = _pending_result(
                state, logical_id, reason, slot, t, allowed_at,
                int(queued.get("attempts") or 0),
                "clock_rollback" if age < 0 else "minimum_interval")
            return {**pending, "coalesced": True}
        attempts = int(queued.get("attempts") or 0) + 1
        state["last_request_at"] = t
        state["stats"]["requests"] += 1
        if attempts > 1:
            state["stats"]["retries"] += 1
        _save_state(state)
        try:
            item = _fetch_once(t)
            old = cached(now=t)
            if (old and _fnum(old.get("updated_at")) is not None and
                    item["updated_at"] < float(old["updated_at"])):
                raise ValueError("response_older_than_cache")
            if item.get("cny_status") != "fresh" and old and old.get("price_gram_cny") is not None:
                # Retain all operands and source/receipt times from ONE complete
                # response. Never multiply the new P by a previous response's R.
                item["current_response_fx_rate"] = item.get("fx_rate")
                item["current_response_fx_source"] = item.get("fx_source")
                for field in ("price_gram_cny", "cny_spot_usd_oz", "cny_fx_rate",
                              "cny_price_as_of", "cny_updated_at", "cny_fetched_at",
                              "fx_rate", "fx_rate_display", "fx_source", "fx_as_of"):
                    item[field] = old.get(field)
                item["cny_status"] = "stale"
                item["fx_stale"] = True
            if item.get("usd_status") == "fresh":
                try:
                    gold_baseline.consider_spot(item, now=t)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[GOLD] baseline update skipped: %s", type(exc).__name__)
            item = _attach_changes_safely(item, now=t)
            cache_status = "fresh" if item["status"] == "ok" else "stale"
            dc.record_success(GROUP, item, status=cache_status,
                              meta={"provider": PROVIDER}, now=t)
            dc.bump_version("gold", _visible_seed(item, cache_status))
            state = _load_state()
            if cache_status == "fresh":
                state["stats"]["success"] += 1
                state["last_success_at"] = t
                state["next_allowed_at"] = 0.0
                if slot:
                    state["scheduled_slots"][slot] = {"at": t, "status": "done"}
                state["logical_requests"][logical_id] = {
                    "at": t, "ok": True, "result": "fetched"}
                state["pending_requests"].pop(logical_id, None)
                _save_state(state)
                return {"ok": True, "terminal": True, "attempted": True,
                        "window": slot, "request_id": logical_id,
                        "status": cache_status, "item": item}
            error = str(item.get("freshness_reason") or "stale_quote")
            state["stats"]["failure"] += 1
            next_at = t + max(minimum_interval, 60 * (2 ** (attempts - 1)))
            pending = _pending_result(state, logical_id, reason, slot, t,
                                      next_at, attempts, error, attempted=True,
                                      item=item)
            return {**pending, "accepted": True, "note": error}
        except Exception as exc:  # noqa: BLE001
            error, _ = _classify_error(exc)
            if isinstance(exc, ValueError) and str(exc) == "response_older_than_cache":
                error = "response_older_than_cache"
            old = cached(now=t)
            status_value = dc.record_failure(GROUP, now=t, note=error)
            if old:
                dc.bump_version("gold", _visible_seed(old, status_value))
            state = _load_state()
            state["stats"]["failure"] += 1
            retry_after = _retry_after(exc, t)
            delay = max(minimum_interval, retry_after or 0,
                        min(300, 60 * (2 ** (attempts - 1))))
            state["next_allowed_at"] = max(float(state.get("next_allowed_at") or 0), t + delay)
            pending = _pending_result(state, logical_id, reason, slot, t,
                                      t + delay, attempts, error,
                                      attempted=True, item=old)
            return {**pending, "note": error, "status": status_value}


def retry_pending(*, now: float | None = None) -> dict:
    """Try at most one due request; caller may run this every 30 seconds."""
    t = float(time.time() if now is None else now)
    state = _load_state()
    if state.get("_state_error") not in (None, "missing"):
        return {"ok": False, "error_class": "state_unavailable"}
    rows = [(key, row) for key, row in (state.get("pending_requests") or {}).items()
            if isinstance(row, dict) and float(row.get("next_at") or 0) <= t]
    if not rows:
        return {"ok": True, "action": "none", "pending_count": len(state.get("pending_requests") or {})}
    key, row = min(rows, key=lambda pair: float(pair[1].get("next_at") or 0))
    return refresh(force=True, reason=str(row.get("reason") or "retry"),
                   request_id=key, now=t)


def recover_baseline_if_due(*, now: float | None = None) -> dict:
    """Use the same provider lock and 60-second floor for intraday recovery."""
    t = float(time.time() if now is None else now)
    with state_store.file_lock(STATE_FILE, operation="xaus-gold-refresh"):
        state = _load_state()
        if state.get("_state_error") not in (None, "missing"):
            return {"attempted": False, "reason": "state_unavailable"}
        if state.get("pending_requests"):
            return {"attempted": False, "reason": "spot_pending"}
        if not gold_baseline.recovery_due(now=t):
            return {"attempted": False, "reason": "not_due"}
        allowed_at = max(float(state.get("last_request_at") or 0) + _minimum_interval(),
                         float(state.get("next_allowed_at") or 0))
        if t < allowed_at:
            return {"attempted": False, "reason": "minimum_interval",
                    "retry_after_s": math.ceil(allowed_at - t)}
        state["last_request_at"] = t
        state["stats"]["requests"] += 1
        _save_state(state)
        result = gold_baseline.maybe_recover_usd(now=t)
        if result.get("status") == "recovered":
            item = cached(now=t)
            if item:
                dc.bump_version("gold", _visible_seed(item, effective_cache_status(now=t)))
        retry_after = result.get("retry_after_s")
        if retry_after is not None:
            state = _load_state()
            state["next_allowed_at"] = max(float(state.get("next_allowed_at") or 0),
                                            t + float(retry_after))
            _save_state(state)
        return result


def cached(*, now: float | None = None) -> Optional[dict]:
    group = dc.get_group(GROUP) or {}
    value = group.get("value")
    if (not isinstance(value, dict) or value.get("provider") != PROVIDER
            or value.get("calculation_schema") != gold_baseline.SCHEMA):
        return None
    t = float(time.time() if now is None else now)
    item = dict(value)
    _, quote_max_age, cache_max_age = _freshness_limits()
    quote_at = _fnum(item.get("price_as_of"))
    fetched_at = _fnum(item.get("fetched_at"))
    reason = item.get("freshness_reason")
    if quote_at is None or t < quote_at - MAX_PROVIDER_CLOCK_SKEW:
        reason = "quote_time_invalid"
    elif t - quote_at > quote_max_age:
        reason = "quote_aged"
    if fetched_at is None or t < fetched_at - MAX_PROVIDER_CLOCK_SKEW:
        reason = "cache_time_invalid"
    elif t - fetched_at > cache_max_age and reason is None:
        reason = "cache_aged"
    if dc.group_status(GROUP) == "stale" and reason is None:
        reason = "last_fetch_failed"
    if reason:
        item.update({"status": "stale", "stale": True,
                     "freshness_reason": reason, "note": reason})
        if reason not in {"fx_invalid", "fx_stale", "conversion_nonfinite"}:
            item["usd_status"] = "stale"
    cny_at = _fnum(item.get("cny_price_as_of"))
    cny_fetched = _fnum(item.get("cny_fetched_at"))
    if item.get("price_gram_cny") is not None and (
            item.get("usd_status") != "fresh" or cny_at is None or cny_fetched is None
            or t - cny_at > quote_max_age or t - cny_fetched > cache_max_age):
        item["cny_status"] = "stale"
    return _attach_changes_safely(item, now=t)


def effective_cache_status(*, now: float | None = None) -> str:
    item = cached(now=now)
    if item and item.get("status") == "stale":
        return "stale"
    return dc.group_status(GROUP)


def status() -> dict:
    state = _load_state()
    return {"provider": PROVIDER, "endpoint": ENDPOINT,
            "cache_status": effective_cache_status(), "item": cached(),
            "stats": state.get("stats", {}), "last_request_at": state.get("last_request_at"),
            "minimum_provider_interval_seconds": _minimum_interval(),
            "midnight_baseline": gold_baseline.status()}
