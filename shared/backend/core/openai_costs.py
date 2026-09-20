"""OpenAI organization monthly-cost collector with complete-page snapshots.

The Admin API key is write-only operator configuration.  A key change always
returns the collector to ``pending_validation``; neither startup nor automatic
polling may use it until the operator explicitly verifies it from the local
console.  Only complete UTC-month snapshots are published.  A failed or partial
refresh keeps the previous complete snapshot for the same organization/month
and marks it stale.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from . import operator_config, state_store

logger = logging.getLogger(__name__)

STATE_FILE = state_store.state_path("openai_costs_state.json")
COSTS_URL = "https://api.openai.com/v1/organization/costs"
INFLIGHT_STALE_SECONDS = 180
MAX_PAGES = 100
_poller_task: asyncio.Task | None = None


class CostsError(RuntimeError):
    def __init__(self, state: str, detail: str, *, status_code: int | None = None):
        self.state = state
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


def configure_state_file(path: Path) -> None:
    global STATE_FILE
    STATE_FILE = Path(path)


def _default() -> dict:
    return {
        "schema": 1,
        "credential": {"status": "missing", "key_fingerprint": None},
        "scopes": {},
        "refresh": {"inflight": None, "last_manual_attempt_at": 0},
    }


def _load() -> dict:
    value, error = state_store.read_json(STATE_FILE)
    if error == "missing":
        return _default()
    if error or not isinstance(value, dict) or value.get("schema") != 1:
        raise state_store.StateStoreError("OpenAI cost state unavailable")
    result = copy.deepcopy(value)
    result.setdefault("credential", {})
    result.setdefault("scopes", {})
    result.setdefault("refresh", {})
    return result


def _secret_and_config() -> tuple[str, dict]:
    effective = operator_config.load_effective()
    key = str((effective._secrets.get("services") or {}).get("openai_admin_api_key") or "").strip()
    return key, dict(effective.config.get("openai_costs") or {})


def _fingerprint(key: str) -> str | None:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] if key else None


def mark_key_changed(key: str) -> None:
    """Invalidate authorization without deleting same-scope historical snapshots."""
    def update(value: dict) -> None:
        value["credential"] = {
            "status": "pending_validation" if key else "missing",
            "key_fingerprint": _fingerprint(key),
            "reason": "key-changed-requires-validation" if key else "api-key-missing",
            "updated_at": int(time.time()),
        }
        value.setdefault("refresh", {})["inflight"] = None
    state_store.update_json(STATE_FILE, update, default=_default())


def _month_bounds(now: datetime | None = None) -> tuple[int, int, str]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return int(start.timestamp()), int(end.timestamp()), start.strftime("%Y-%m")


def _scope_id(organization_id: str | None, month: str) -> str:
    return f"{organization_id or 'selected'}|{month}"


def credential_state() -> dict:
    key, _ = _secret_and_config()
    fp = _fingerprint(key)
    try:
        state = _load()
    except state_store.StateStoreError:
        return {"state": "state_unavailable", "configured": bool(key)}
    saved = state.get("credential") or {}
    if not key:
        return {"state": "missing", "configured": False}
    if saved.get("key_fingerprint") != fp:
        return {"state": "pending_validation", "configured": True}
    return {
        "state": str(saved.get("status") or "pending_validation"),
        "configured": True,
        "reason": saved.get("reason"),
        "updated_at": saved.get("updated_at"),
    }


def snapshot(now: datetime | None = None) -> dict:
    """Return current UTC-month display state without making a network request."""
    key, cfg = _secret_and_config()
    start, end, month = _month_bounds(now)
    organization = str(cfg.get("organization_id") or "").strip() or None
    scope = _scope_id(organization, month)
    try:
        state = _load()
    except state_store.StateStoreError:
        return {
            "state": "state_unavailable", "complete": False, "amount": None,
            "currency": "USD", "month": month, "utc_month": True,
        }
    row = (state.get("scopes") or {}).get(scope)
    credential = credential_state()
    current_key_fingerprint = _fingerprint(key)
    saved_credential = state.get("credential") or {}
    row_key_fingerprint = row.get("key_fingerprint") if isinstance(row, dict) else None
    if row_key_fingerprint is None and saved_credential.get("status") == "active":
        # Schema-1 snapshots written before per-row binding are safe only while
        # the still-active credential fingerprint is unchanged.
        row_key_fingerprint = saved_credential.get("key_fingerprint")
    if (
        not isinstance(row, dict)
        or row.get("complete") is not True
        or row_key_fingerprint != current_key_fingerprint
    ):
        return {
            "state": credential["state"], "complete": False, "amount": None,
            "currency": "USD", "month": month, "utc_month": True,
            "start_time": start, "end_time": end,
        }
    return {
        "state": "stale" if row.get("stale") else "current",
        "complete": True,
        "amount": str(row.get("amount")),
        "currency": str(row.get("currency") or "USD").upper(),
        "month": month,
        "utc_month": True,
        "start_time": start,
        "end_time": end,
        "checked_at": row.get("checked_at"),
        "stale": bool(row.get("stale")),
        "last_error": row.get("last_error"),
        "organization_scope": "configured" if organization else "selected",
    }


def _error_from_response(response: httpx.Response) -> CostsError:
    status = response.status_code
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = {}
    message = str(((body.get("error") or {}).get("message") if isinstance(body, dict) else "") or "")
    lower = message.lower()
    if status == 401:
        state = "unauthorized"
    elif status == 403:
        state = "forbidden"
    elif status == 429:
        state = "rate_limited"
    elif "organization" in lower or "org" in lower:
        state = "organization_mismatch"
    elif status >= 500:
        state = "temporary_failure"
    else:
        state = "request_rejected"
    return CostsError(state, message[:240] or f"HTTP {status}", status_code=status)


async def _collect_complete(key: str, cfg: dict, *, now: datetime | None = None,
                            client: httpx.AsyncClient | None = None) -> dict:
    start, end, month = _month_bounds(now)
    organization = str(cfg.get("organization_id") or "").strip() or None
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if organization:
        headers["OpenAI-Organization"] = organization
    params: dict[str, Any] = {
        "start_time": start,
        "end_time": end,
        "bucket_width": "1d",
        "limit": 180,
    }
    owned = client is None
    http = client or httpx.AsyncClient(timeout=20.0)
    page: str | None = None
    seen_pages: set[str] = set()
    total = Decimal("0")
    page_count = 0
    bucket_count = 0
    try:
        while True:
            request_params = dict(params)
            if page:
                request_params["page"] = page
            try:
                response = await http.get(COSTS_URL, headers=headers, params=request_params)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise CostsError("network", type(exc).__name__) from exc
            if response.status_code != 200:
                raise _error_from_response(response)
            try:
                payload = response.json()
            except Exception as exc:  # noqa: BLE001
                raise CostsError("invalid_response", "response is not JSON") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise CostsError("invalid_response", "missing data buckets")
            page_count += 1
            if page_count > MAX_PAGES:
                raise CostsError("pagination_error", "page limit exceeded")
            for bucket in payload["data"]:
                if not isinstance(bucket, dict):
                    raise CostsError("invalid_response", "invalid cost bucket")
                bucket_count += 1
                for result in bucket.get("results") or []:
                    amount = result.get("amount") if isinstance(result, dict) else None
                    if not isinstance(amount, dict):
                        raise CostsError("invalid_response", "missing amount")
                    currency = str(amount.get("currency") or "").lower()
                    if currency != "usd":
                        raise CostsError("invalid_response", f"unsupported currency {currency or 'missing'}")
                    try:
                        total += Decimal(str(amount.get("value")))
                    except (InvalidOperation, TypeError, ValueError) as exc:
                        raise CostsError("invalid_response", "invalid amount value") from exc
            has_more = payload.get("has_more") is True
            next_page = payload.get("next_page")
            if not has_more:
                break
            if not isinstance(next_page, str) or not next_page or next_page in seen_pages:
                raise CostsError("pagination_error", "missing or repeated next_page")
            seen_pages.add(next_page)
            page = next_page
    finally:
        if owned:
            await http.aclose()
    return {
        "scope": _scope_id(organization, month),
        "month": month,
        "start_time": start,
        "end_time": end,
        "organization_id": organization,
        # Plain decimal text avoids exponent notation and preserves every
        # source digit across JSON/JavaScript/firmware boundaries.
        "amount": format(total, "f"),
        "currency": "USD",
        "complete": True,
        "page_count": page_count,
        "bucket_count": bucket_count,
    }


def _release(owner: str, *, success: dict | None = None,
             error: CostsError | None = None, verify: bool = False) -> None:
    now = int(time.time())
    def update(state: dict) -> None:
        inflight = (state.get("refresh") or {}).get("inflight") or {}
        if inflight.get("owner") != owner:
            return
        state.setdefault("refresh", {})["inflight"] = None
        credential = state.setdefault("credential", {})
        if success is not None:
            row = copy.deepcopy(success)
            row.update({
                "checked_at": now,
                "stale": False,
                "last_error": None,
                "key_fingerprint": credential.get("key_fingerprint"),
            })
            state.setdefault("scopes", {})[success["scope"]] = row
            if verify:
                credential.update({"status": "active", "reason": "verified-costs-read",
                                   "updated_at": now})
        elif error is not None:
            scope = inflight.get("scope")
            previous = (state.get("scopes") or {}).get(scope)
            if (
                isinstance(previous, dict)
                and previous.get("complete") is True
                and previous.get("key_fingerprint") == credential.get("key_fingerprint")
            ):
                previous["stale"] = True
                previous["last_error"] = error.state
                previous["last_attempt_at"] = now
            # Only credential/scope failures revoke authorization. Transient
            # transport and service errors keep a previously verified key
            # active so the background poller can recover automatically.
            if error.state in {"unauthorized", "forbidden", "organization_mismatch"}:
                credential.update({"status": error.state, "reason": error.state,
                                   "updated_at": now})
            elif credential.get("status") != "active":
                credential.update({"status": error.state, "reason": error.state,
                                   "updated_at": now})
    state_store.update_json(STATE_FILE, update, default=_default())


async def refresh(*, reason: str = "auto", verify: bool = False,
                  force: bool = False, now: datetime | None = None,
                  client: httpx.AsyncClient | None = None) -> dict:
    key, cfg = _secret_and_config()
    if not key:
        return {"ok": False, "state": "missing", "network_called": False}
    credential = credential_state()
    if not verify and credential.get("state") != "active":
        return {"ok": False, "state": credential.get("state") or "pending_validation",
                "network_called": False}
    _, _, month = _month_bounds(now)
    organization = str(cfg.get("organization_id") or "").strip() or None
    scope = _scope_id(organization, month)
    current = time.time()
    owner = uuid.uuid4().hex
    def reserve(state: dict) -> dict:
        inflight = (state.get("refresh") or {}).get("inflight")
        if isinstance(inflight, dict) and current - float(inflight.get("at") or 0) < INFLIGHT_STALE_SECONDS:
            return {"reserved": False, "result": {
                "ok": True, "state": "coalesced", "network_called": False}}
        last = (state.get("scopes") or {}).get(scope) or {}
        interval = max(5, int(cfg.get("poll_interval_minutes") or 60)) * 60
        if reason == "auto" and not force and current - float(last.get("checked_at") or 0) < interval:
            return {"reserved": False, "result": {
                "ok": True, "state": "not_due", "network_called": False}}
        cooldown = max(10, int(cfg.get("manual_refresh_cooldown_seconds") or 60))
        last_manual = float((state.get("refresh") or {}).get("last_manual_attempt_at") or 0)
        if reason == "manual" and not force and current - last_manual < cooldown:
            return {"reserved": False, "result": {
                "ok": False, "state": "cooldown",
                "retry_after": int(cooldown - (current - last_manual)),
                "network_called": False}}
        state.setdefault("credential", {}).update({"key_fingerprint": _fingerprint(key)})
        state.setdefault("refresh", {})["inflight"] = {
            "owner": owner, "at": current, "reason": reason, "scope": scope,
        }
        if reason == "manual":
            state["refresh"]["last_manual_attempt_at"] = current
        return {"reserved": True}
    reservation = state_store.update_json(STATE_FILE, reserve, default=_default())
    if not reservation.get("reserved"):
        return reservation["result"]
    try:
        complete = await _collect_complete(key, cfg, now=now, client=client)
    except CostsError as exc:
        _release(owner, error=exc, verify=verify)
        return {"ok": False, "state": exc.state, "network_called": True,
                "status_code": exc.status_code}
    except Exception as exc:  # noqa: BLE001
        failure = CostsError("temporary_failure", type(exc).__name__)
        _release(owner, error=failure, verify=verify)
        logger.warning("[OPENAI_COSTS] refresh failed: %s", type(exc).__name__)
        return {"ok": False, "state": failure.state, "network_called": True}
    _release(owner, success=complete, verify=verify)
    return {"ok": True, "state": "current", "network_called": True,
            "snapshot": snapshot(now)}


async def _poll_loop() -> None:
    while True:
        try:
            await refresh(reason="auto")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[OPENAI_COSTS] poll skipped: %s", type(exc).__name__)
        await asyncio.sleep(30)


def start_poller() -> asyncio.Task:
    global _poller_task
    if _poller_task is None or _poller_task.done():
        _poller_task = asyncio.create_task(_poll_loop(), name="openai-costs-poller")
    return _poller_task


async def stop_poller() -> None:
    global _poller_task
    task, _poller_task = _poller_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
