from __future__ import annotations

import copy

from core import activity_signals
from core import deepseek_activity as activity


def _ai(raw: float, effective: float) -> dict:
    return {
        "codex_7d_used": 20.0,
        "codex_resets_at": 2_000_000_000,
        "reset_credits_available": 1,
        "reset_expiry_list": [2_000_000_100],
        "plan": "PLUS",
        "valid_until_date": "2026-10-23",
        "member": {"plan": "PLUS", "valid_until_date": "2026-10-23"},
        "deepseek": {"CNY": raw, "USD": 0.0},
        "deepseek_activity": {"CNY": effective, "USD": 0.0},
    }


def _use(tmp_path):
    previous = activity._STATE_FILE
    activity.configure_state_file(tmp_path / "deepseek_activity.json")
    return previous


def test_scheduled_charge_updates_display_without_activity_switch(tmp_path):
    previous = _use(tmp_path)
    try:
        assert activity.observe_balances({"CNY": 1.00, "USD": 0.0}, now=100)["CNY"] == 1.0
        assert activity.reserve_automatic_call(
            "2026-09-23|morning", "2026-09-23|morning#1", 0.02, now=110)
        assert activity.settle_automatic_call("2026-09-23|morning#1", 0.004623, now=120)
        effective = activity.observe_balances({"CNY": 0.99, "USD": 0.0}, now=250)
        before = _ai(1.0, 1.0)
        after = _ai(0.99, effective["CNY"])
        assert after["deepseek"]["CNY"] == 0.99
        assert activity_signals.activity_key(before) == activity_signals.activity_key(after)
        assert activity_signals.ai_visual_key(before) != activity_signals.ai_visual_key(after)
    finally:
        activity.configure_state_file(previous)


def test_external_or_manual_usage_remains_activity(tmp_path):
    previous = _use(tmp_path)
    try:
        activity.observe_balances({"CNY": 1.00, "USD": 0.0}, now=100)
        # Manual issue IDs are intentionally ineligible for suppression.
        assert not activity.reserve_automatic_call(
            "manual|task-1", "manual|task-1#1", 0.02, now=110)
        effective = activity.observe_balances({"CNY": 0.99, "USD": 0.0}, now=120)
        assert effective["CNY"] == 0.99
        assert activity_signals.activity_key(_ai(1.0, 1.0)) != activity_signals.activity_key(
            _ai(0.99, 0.99))
    finally:
        activity.configure_state_file(previous)


def test_large_unrelated_drop_is_not_hidden(tmp_path):
    previous = _use(tmp_path)
    try:
        activity.observe_balances({"CNY": 1.00, "USD": 0.0}, now=100)
        activity.reserve_automatic_call("2026-09-23|evening", "call-1", 0.02, now=110)
        activity.settle_automatic_call("call-1", 0.004, now=120)
        assert activity.observe_balances({"CNY": 0.95, "USD": 0.0}, now=180)["CNY"] == 0.95
    finally:
        activity.configure_state_file(previous)


def test_uncertain_network_charge_is_bounded_then_released(tmp_path):
    previous = _use(tmp_path)
    try:
        activity.observe_balances({"CNY": 1.00, "USD": 0.0}, now=100)
        activity.reserve_automatic_call("2026-09-23|morning", "call-timeout", 0.02, now=110)
        assert activity.observe_balances({"CNY": 0.99, "USD": 0.0}, now=200)["CNY"] == 1.0
        assert activity.effective_balances({"CNY": 0.99, "USD": 0.0}, now=2_000)["CNY"] == 0.99
    finally:
        activity.configure_state_file(previous)


def test_late_settlement_confirms_provisional_charge(tmp_path):
    previous = _use(tmp_path)
    try:
        activity.observe_balances({"CNY": 1.00, "USD": 0.0}, now=100)
        activity.reserve_automatic_call("2026-09-23|morning", "call-late", 0.02, now=110)
        activity.observe_balances({"CNY": 0.99, "USD": 0.0}, now=200)
        assert activity.settle_automatic_call("call-late", 0.0046, now=300)
        assert activity.effective_balances({"CNY": 0.99, "USD": 0.0}, now=2_000)["CNY"] == 1.0
    finally:
        activity.configure_state_file(previous)


def test_retry_costs_and_restart_state_are_persistent(tmp_path):
    previous = _use(tmp_path)
    try:
        path = tmp_path / "deepseek_activity.json"
        activity.observe_balances({"CNY": 1.00, "USD": 0.0}, now=100)
        for number in (1, 2):
            call_id = f"2026-09-23|morning#{number}"
            activity.reserve_automatic_call("2026-09-23|morning", call_id, 0.02, now=110 + number)
            activity.settle_automatic_call(call_id, 0.004, now=120 + number)
        # Reconfiguration simulates a new worker/process reading the same file.
        activity.configure_state_file(path)
        assert activity.observe_balances({"CNY": 0.99, "USD": 0.0}, now=300)["CNY"] == 1.0
        assert activity.observe_balances({"CNY": 1.10, "USD": 0.0}, now=400)["CNY"] == 1.1
    finally:
        activity.configure_state_file(previous)


def test_projection_does_not_change_visual_keys_when_only_effective_changes():
    raw = _ai(0.99, 1.0)
    other = copy.deepcopy(raw)
    other["deepseek_activity"]["CNY"] = 0.98
    assert activity_signals.activity_key(raw) != activity_signals.activity_key(other)
    assert activity_signals.ai_visual_key(raw) == activity_signals.ai_visual_key(other)
