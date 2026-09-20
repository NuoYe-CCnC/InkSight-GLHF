"""Explicit, cost-free validation of the configured news API credential."""
from __future__ import annotations

import time

import httpx

from . import news_brief, news_schedule, state_store

_BALANCE_URL = "https://api.deepseek.com/user/balance"


def validate_configured_key() -> dict:
    key = news_brief._env_key()
    if not key:
        result = {"state": "missing", "authorized": False, "key_configured": False,
                  "reason": "api-key-missing", "checked_at": int(time.time())}
        state_store.write_json(news_schedule.AUTHORIZED_FILE, result)
        return result
    try:
        response = httpx.get(
            _BALANCE_URL,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=15.0,
        )
    except (httpx.TimeoutException, httpx.NetworkError):
        result = {"state": "temporary_failure", "authorized": False,
                  "key_configured": True, "reason": "network", "checked_at": int(time.time())}
    else:
        if response.status_code == 200:
            result = {"state": "active", "authorized": True, "key_configured": True,
                      "key_status": "active", "reason": "validated-enabled",
                      "checked_at": int(time.time())}
        elif response.status_code in {401, 403}:
            result = {"state": "auth_failure", "authorized": False, "key_configured": True,
                      "key_status": "auth_failure", "reason": "credential-rejected",
                      "checked_at": int(time.time())}
        else:
            result = {"state": "temporary_failure", "authorized": False,
                      "key_configured": True, "key_status": "temporary_failure",
                      "reason": f"http-{response.status_code}", "checked_at": int(time.time())}
    state_store.write_json(news_schedule.AUTHORIZED_FILE, result)
    return result
