"""Source freshness policy derived from the actual collector schedule."""
from __future__ import annotations

import json
import os
from pathlib import Path

_DEFAULT_POLICY = {
    "publish_sec": 60,
    "codex_open_sec": 60,
    "codex_closed_sec": 600,
}
_POLICY_FILE = Path(__file__).resolve().parents[2] / "tools" / "agent_policy.json"


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _agent_policy() -> dict:
    policy = dict(_DEFAULT_POLICY)
    path = Path(os.getenv("INK_AGENT_POLICY", str(_POLICY_FILE)))
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        loaded = {}
    if isinstance(loaded, dict):
        for key, default in _DEFAULT_POLICY.items():
            policy[key] = _positive_int(loaded.get(key), default)
    return policy


def ai_source_policy() -> dict:
    """Return per-source expected period and stale threshold in seconds.

    The default rule is stale only after three consecutive expected collection
    cycles have elapsed without a successful source sample.
    """
    policy = _agent_policy()
    missed = _positive_int(os.getenv("INK_FRESH_MISSED_CYCLES"), 3)
    codex_period = _positive_int(
        os.getenv("INK_FRESH_CODEX_PERIOD_SEC"),
        max(policy["codex_open_sec"], policy["codex_closed_sec"]),
    )
    ds_period = _positive_int(
        os.getenv("INK_FRESH_DS_PERIOD_SEC"), policy["publish_sec"])

    def row(period: int) -> dict:
        return {"expected_period_s": period,
                "missed_cycles": missed,
                "stale_after_s": period * missed}

    return {
        "codex": row(codex_period),
        "ds_cny": row(ds_period),
        "ds_usd": row(ds_period),
    }
