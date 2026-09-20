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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import data_cache as dc
from . import gold_baseline
from . import state_store

logger = logging.getLogger(__name__)

GROUP = "gold.xaus.spot.cny_gram"
ENDPOINT = "https://xaus.com/api/v1/spot?currency=CNY&unit=gram&compact=1"
PROVIDER = "xaus.com"
INSTRUMENT = {"provider": PROVIDER, "instrument": "XAU", "instrument_id": "XAU",
              "instrument_label": "伦敦金参考价", "currency": "CNY", "unit": "g"}
PENDING_NOTE = None
STATE_FILE = state_store.state_path("xaus_gold_state.json")
MIN_REQUEST_INTERVAL_S = 30
MAX_PROVIDER_CLOCK_SKEW = 300
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
            "logical_requests": {},
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
    for key, keep in (("scheduled_slots", 96), ("logical_requests", 256)):
        rows = clean.get(key) or {}
        if len(rows) > keep:
            ordered = sorted(rows.items(), key=lambda pair: float((pair[1] or {}).get("at", 0)))
            clean[key] = dict(ordered[-keep:])
    state_store.write_json(STATE_FILE, clean)


def _fnum(value) -> Optional[float]:
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


def _parse(data: dict, fetched_at: float) -> dict:
    xau = data.get("xau")
    if not isinstance(xau, dict):
        raise ValueError("missing xau object")
    currency = str(xau.get("currency") or "").upper()
    unit = str(xau.get("unit") or "").lower()
    cny_g = _fnum(xau.get("price"))
    usd_oz = _fnum(data.get("spot_usd_oz"))
    fx = _fnum(data.get("fx_rate"))
    if currency != "CNY" or unit not in {"gram", "g"}:
        raise ValueError(f"unexpected xau unit {currency}/{unit}")
    if cny_g is None or cny_g <= 0 or usd_oz is None or usd_oz <= 0 or fx is None or fx <= 0:
        raise ValueError("non-positive or non-finite price/fx")
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
        if stamp > now_i + MAX_PROVIDER_CLOCK_SKEW:
            raise ValueError(f"future {label}")
    provider_stale = bool(data.get("stale")) or bool(data.get("fx_stale"))
    state_status = str(state.get("status") or ("stale" if provider_stale else "fresh")).lower()
    if state_status not in {"fresh", "stale", "degraded", "unknown", "error"}:
        state_status = "unknown"
    return {
        "provider": PROVIDER, "instrument_id": "XAU",
        "instrument_label": "伦敦金参考价",
        "spot_usd_oz": usd_oz, "price_gram_cny": cny_g,
        "currency": "CNY", "unit": "g", "fx_rate": fx,
        "fx_rate_display": round(fx, 2),
        "price_source": str(data.get("price_source") or "unknown"),
        "price_as_of": price_as_of,
        "quote_time": price_as_of,  # old firmware compatibility; same honest source time
        "data_state": {"status": state_status, "as_of": state_as_of,
                       "source": str(state.get("source") or "unknown")},
        "stale": bool(data.get("stale")),
        "fx_source": str(data.get("fx_source") or "unknown"),
        "fx_stale": bool(data.get("fx_stale")), "fx_as_of": None,
        "updated_at": updated_at, "fetched_at": now_i,
        "status": "stale" if provider_stale or state_status != "fresh" else "ok",
        "note": None,
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
    return {"spot_usd_oz": round(float(item["spot_usd_oz"]), 2),
            "price_gram_cny": round(float(item["price_gram_cny"]), 2),
            "fx_rate": round(float(item["fx_rate"]), 2),
            "price_source": item.get("price_source"),
            "price_as_of": item.get("price_as_of"),
            "baseline_date": item.get("baseline_date"),
            "baseline_method": item.get("baseline_method"),
            "baseline_price_as_of": item.get("baseline_price_as_of"),
            "baseline_cny_kind": item.get("baseline_cny_kind"),
            "baseline_cny_price_as_of": item.get("baseline_cny_price_as_of"),
            "change_usd_oz": _rounded_optional(
                item.get("change_usd_oz_since_bj_midnight")),
            "change_cny_g": _rounded_optional(
                item.get("change_cny_g_since_reference",
                         item.get("change_cny_g_since_bj_midnight"))),
            "change_status": item.get("change_status"),
            "data_state": (item.get("data_state") or {}).get("status"),
            "stale": bool(item.get("stale")), "fx_source": item.get("fx_source"),
            "fx_stale": bool(item.get("fx_stale")), "cache_status": cache_status}


def visual_seed(item: dict, cache_status: str | None = None) -> dict:
    """Public redraw seed shared by scheduled and document build paths."""
    return _visible_seed(item, cache_status or dc.group_status(GROUP) or "unknown")


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
    status, data = _request()
    if status != 200:
        raise urllib.error.HTTPError(ENDPOINT, status, "unexpected status", {}, None)
    if not data:
        raise ValueError("empty/non-object JSON")
    return _parse(data, fetched_at)


def _scheduled_slot(now: datetime | None = None) -> str:
    current = now or _bj_now()
    minute = 0 if current.minute < 30 else 30
    return current.replace(minute=minute, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M%z")


def _mode_enabled() -> bool:
    try:
        from .operator_config import load_effective
        from .news_calendar import is_workday
        config = load_effective().config
        gold = config.get("gold_refresh") or {}
        if not gold.get("scheduled_enabled", True):
            return False
        now = _bj_now()
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


def refresh(force: bool = False, *, reason: str = "scheduled",
            request_id: str | None = None, now: float | None = None) -> dict:
    """Refresh with persistent slot/idempotency and 30-second coalescing."""
    t = float(now if now is not None else time.time())
    logical_id = str(request_id or "")[:96]
    with state_store.file_lock(STATE_FILE, operation="xaus-gold-refresh"):
        state = _load_state()
        if reason in {"scheduled", "service-start", "host-wake"} and not force:
            if not _mode_enabled():
                return {"ok": False, "disabled": True, "status": dc.group_status(GROUP),
                        "item": cached(now=t)}
            slot = _scheduled_slot()
            if (state.get("scheduled_slots") or {}).get(slot, {}).get("status") == "done":
                return {"ok": False, "window_done": True, "window": slot,
                        "status": dc.group_status(GROUP), "item": cached(now=t)}
        else:
            slot = None
        prior = (state.get("logical_requests") or {}).get(logical_id) if logical_id else None
        if isinstance(prior, dict):
            state["stats"]["deduped"] += 1
            _save_state(state)
            return {"ok": bool(prior.get("ok")), "deduped": True,
                    "request_id": logical_id, "status": dc.group_status(GROUP),
                    "item": cached(now=t)}
        age = t - float(state.get("last_request_at") or 0)
        minimum_interval = _minimum_interval()
        if 0 <= age < minimum_interval:
            state["stats"]["coalesced"] += 1
            cached_item = cached(now=t)
            if logical_id:
                state["logical_requests"][logical_id] = {
                    "at": t, "ok": cached_item is not None, "result": "coalesced"}
            _save_state(state)
            return {"ok": cached_item is not None, "coalesced": True,
                    "retry_after_s": max(1, int(minimum_interval - age)),
                    "request_id": logical_id or None,
                    "status": dc.group_status(GROUP), "item": cached_item}
        state["last_request_at"] = t
        state["stats"]["requests"] += 1
        _save_state(state)

        last_error = "unknown"
        for attempt in range(2):
            try:
                item = _fetch_once(t)
                try:
                    gold_baseline.consider_spot(item, now=t)
                    if reason in {"scheduled", "service-start", "host-wake"}:
                        gold_baseline.maybe_recover_usd(now=t)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[GOLD] baseline update skipped: %s", exc)
                item = _attach_changes_safely(item, now=t)
                cache_status = "stale" if item["status"] == "stale" else "fresh"
                dc.record_success(GROUP, item, status=cache_status,
                                  meta={"provider": PROVIDER}, now=t)
                dc.bump_version("gold", _visible_seed(item, cache_status))
                state = _load_state()
                state["stats"]["success"] += 1
                if slot:
                    state["scheduled_slots"][slot] = {"at": t, "status": "done"}
                if logical_id:
                    state["logical_requests"][logical_id] = {
                        "at": t, "ok": True, "result": "fetched"}
                _save_state(state)
                return {"ok": True, "window": slot, "request_id": logical_id or None,
                        "status": cache_status, "item": item}
            except Exception as exc:  # noqa: BLE001
                last_error, transient = _classify_error(exc)
                if not transient or attempt:
                    break
                state = _load_state()
                state["stats"]["retries"] += 1
                state["stats"]["requests"] += 1
                _save_state(state)

        old = cached(now=t)
        status_value = dc.record_failure(GROUP, now=t, note=last_error)
        if old:
            dc.bump_version("gold", _visible_seed(old, status_value))
        state = _load_state()
        state["stats"]["failure"] += 1
        if logical_id:
            state["logical_requests"][logical_id] = {
                "at": t, "ok": False, "result": last_error}
        _save_state(state)
        return {"ok": False, "error_class": last_error, "note": last_error,
                "request_id": logical_id or None, "status": status_value, "item": old}


def cached(*, now: float | None = None) -> Optional[dict]:
    group = dc.get_group(GROUP) or {}
    value = group.get("value")
    if not isinstance(value, dict) or value.get("provider") != PROVIDER:
        return None
    return _attach_changes_safely(value, now=now)


def status() -> dict:
    state = _load_state()
    return {"provider": PROVIDER, "endpoint": ENDPOINT,
            "cache_status": dc.group_status(GROUP), "item": cached(),
            "stats": state.get("stats", {}), "last_request_at": state.get("last_request_at"),
            "minimum_provider_interval_seconds": _minimum_interval(),
            "midnight_baseline": gold_baseline.status()}
