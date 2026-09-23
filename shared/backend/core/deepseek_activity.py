"""Attribute DeepSeek balance changes without hiding the displayed balance.

Scheduled news generation is an internal background action.  Its provider
charge must still be displayed, but must not be treated as a user-visible AI
activity that changes the e-paper page.  Reservations and settlements live in
their own crash-safe state so delayed provider balance updates can be matched
across processes and restarts.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import state_store


_STATE_FILE = state_store.state_path("deepseek_activity.json")
_EXACT_WINDOW_S = 6 * 60 * 60
_UNCERTAIN_WINDOW_S = 30 * 60
_ROUNDING_TOLERANCE_CNY = 0.011
_MAX_PENDING = 40
_MAX_LOG = 120


def configure_state_file(path: str | Path) -> None:
    global _STATE_FILE
    _STATE_FILE = Path(path)


def _default() -> dict:
    return {
        "last_raw": {"CNY": None, "USD": None},
        "effective": {"CNY": None, "USD": None},
        "pending": [],
        "provisional": [],
        "log": [],
    }


def _money(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError, OverflowError):
        return None


def _log(state: dict, now: int, kind: str, **details: Any) -> None:
    state.setdefault("log", []).append({"at": now, "kind": kind, **details})
    state["log"] = state["log"][-_MAX_LOG:]


def _prune(state: dict, now: int) -> None:
    state["pending"] = [
        row for row in (state.get("pending") or [])
        if isinstance(row, dict)
        and (not row.get("consumed") or row.get("held"))
        and int(row.get("expires_at") or 0) > now
    ][-_MAX_PENDING:]


def reserve_automatic_call(issue_key: str, call_id: str, max_cost_cny: float,
                           *, now: int | None = None) -> bool:
    """Persist a bounded reservation immediately before automatic network I/O."""
    if not issue_key or issue_key.startswith("manual|") or not call_id:
        return False
    stamp = int(now or time.time())
    maximum = max(0.0, _money(max_cost_cny) or 0.0)

    def mutate(state: dict) -> bool:
        _prune(state, stamp)
        if any(row.get("call_id") == call_id for row in state.get("pending") or []):
            return True
        state.setdefault("pending", []).append({
            "issue_key": issue_key,
            "call_id": call_id,
            "reserved_at": stamp,
            "max_cost_cny": maximum,
            "exact_cost_cny": None,
            "settled_at": None,
            "expires_at": stamp + _UNCERTAIN_WINDOW_S,
            "consumed": False,
        })
        state["pending"] = state["pending"][-_MAX_PENDING:]
        _log(state, stamp, "reserve", call_id=call_id, issue_key=issue_key,
             max_cost_cny=maximum)
        return True

    return state_store.update_json(_STATE_FILE, mutate, default=_default())


def settle_automatic_call(call_id: str, exact_cost_cny: float,
                          *, now: int | None = None) -> bool:
    """Replace an uncertain reservation with the actual billed-token estimate."""
    if not call_id:
        return False
    stamp = int(now or time.time())
    exact = max(0.0, _money(exact_cost_cny) or 0.0)

    def mutate(state: dict) -> bool:
        found = False
        for row in state.get("pending") or []:
            if row.get("call_id") != call_id:
                continue
            row["exact_cost_cny"] = exact
            row["settled_at"] = stamp
            row["expires_at"] = stamp + _EXACT_WINDOW_S
            row["held"] = False
            if any(call_id in (item.get("call_ids") or [])
                   for item in state.get("provisional") or []):
                row["consumed"] = True
            found = True
            break
        if not found:
            return False
        # If a balance drop arrived while the request outcome was uncertain,
        # settlement makes that provisional suppression permanent.
        for provisional in state.get("provisional") or []:
            if call_id in (provisional.get("call_ids") or []):
                provisional["confirmed"] = True
        _log(state, stamp, "settle", call_id=call_id, exact_cost_cny=exact)
        return True

    return state_store.update_json(_STATE_FILE, mutate, default=_default())


def _release_expired_provisional(state: dict, now: int) -> bool:
    kept = []
    released = False
    for row in state.get("provisional") or []:
        if row.get("confirmed"):
            continue
        if int(row.get("expires_at") or 0) <= now:
            raw = _money((state.get("last_raw") or {}).get("CNY"))
            if raw is not None:
                state.setdefault("effective", {})["CNY"] = raw
            _log(state, now, "release-unconfirmed", delta_cny=row.get("delta_cny"))
            released = True
        else:
            kept.append(row)
    state["provisional"] = kept
    return released


def observe_balances(balances: dict, *, now: int | None = None) -> dict:
    """Record one successful full provider snapshot and return activity balances."""
    stamp = int(now or time.time())
    incoming = {cur: _money(balances.get(cur)) for cur in ("CNY", "USD")}

    def mutate(state: dict) -> dict:
        state.setdefault("last_raw", {"CNY": None, "USD": None})
        state.setdefault("effective", {"CNY": None, "USD": None})
        state.setdefault("pending", [])
        state.setdefault("provisional", [])
        state.setdefault("log", [])
        _release_expired_provisional(state, stamp)
        _prune(state, stamp)

        previous = dict(state["last_raw"])
        if all(_money(previous.get(cur)) is None for cur in ("CNY", "USD")):
            for cur, value in incoming.items():
                if value is not None:
                    state["last_raw"][cur] = value
                    state["effective"][cur] = value
            _log(state, stamp, "baseline", balances=incoming)
            return dict(state["effective"])

        for cur, value in incoming.items():
            if value is None:
                continue
            old = _money(previous.get(cur))
            state["last_raw"][cur] = value
            if old is None:
                state["effective"][cur] = value
                continue
            if abs(value - old) < 0.0000005:
                continue
            # Only CNY deductions can be attributed to the configured news
            # model. Recharges, USD changes and every increase remain activity.
            if cur != "CNY" or value > old:
                state["effective"][cur] = value
                _log(state, stamp, "external-change", currency=cur,
                     previous=old, current=value)
                continue

            delta = round(old - value, 6)
            exact = [row for row in state["pending"]
                     if row.get("exact_cost_cny") is not None
                     and not row.get("consumed") and not row.get("held")]
            uncertain = [row for row in state["pending"]
                         if row.get("exact_cost_cny") is None
                         and not row.get("consumed") and not row.get("held")]
            exact_allowance = sum(float(row.get("exact_cost_cny") or 0) for row in exact)
            uncertain_allowance = sum(float(row.get("max_cost_cny") or 0) for row in uncertain)
            candidates = exact if delta <= exact_allowance + _ROUNDING_TOLERANCE_CNY else []
            provisional = False
            if not candidates and delta <= uncertain_allowance + _ROUNDING_TOLERANCE_CNY:
                candidates = uncertain
                provisional = True
            if candidates:
                ids = [str(row.get("call_id")) for row in candidates]
                if provisional:
                    for row in candidates:
                        row["held"] = True
                    state["provisional"].append({
                        "at": stamp,
                        "expires_at": max(int(row.get("expires_at") or stamp)
                                          for row in candidates),
                        "delta_cny": delta,
                        "call_ids": ids,
                        "confirmed": False,
                    })
                else:
                    for row in candidates:
                        row["consumed"] = True
                _log(state, stamp, "suppress-news-charge", delta_cny=delta,
                     call_ids=ids, provisional=provisional)
            else:
                state["effective"][cur] = value
                _log(state, stamp, "external-change", currency=cur,
                     previous=old, current=value)
        _prune(state, stamp)
        return dict(state["effective"])

    return state_store.update_json(_STATE_FILE, mutate, default=_default())


def effective_balances(raw_balances: dict, *, now: int | None = None) -> dict:
    """Return balances used only by the activity key; raw display is untouched."""
    stamp = int(now or time.time())

    def mutate(state: dict) -> dict:
        state.setdefault("last_raw", {"CNY": None, "USD": None})
        state.setdefault("effective", {"CNY": None, "USD": None})
        state.setdefault("pending", [])
        state.setdefault("provisional", [])
        state.setdefault("log", [])
        _release_expired_provisional(state, stamp)
        _prune(state, stamp)
        out = {}
        for cur in ("CNY", "USD"):
            value = _money(state["effective"].get(cur))
            if value is None:
                value = _money(raw_balances.get(cur))
            out[cur] = value
        return out

    return state_store.update_json(_STATE_FILE, mutate, default=_default())
