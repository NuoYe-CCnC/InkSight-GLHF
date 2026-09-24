"""Public regression checks for the 2026-09-24 activity and token fix."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from core import activity_signals as signals  # noqa: E402
import misans_panel_render as preview  # noqa: E402


def _ai(reset_at: int) -> dict:
    return {
        "codex_7d_used": 59.0,
        "codex_resets_at": reset_at,
        "reset_credits_available": 1,
        "reset_expiry_list": [reset_at + 3600],
        "plan": "PLUS",
        "valid_until_date": "2026-09-23",
        "member": {
            "plan": "plus",
            "valid_until_date": "2026-09-23",
            "renewal_status": "cancelled",
            "precision": "date",
        },
        "deepseek": {"CNY": 9.03, "USD": 0.0},
        "fresh": {
            "codex": 1_790_238_500,
            "ds_cny": 1_790_238_500,
            "ds_usd": 1_790_238_500,
        },
    }


def test_reset_timestamp_second_jitter_does_not_switch_pages():
    first = _ai(1_700_000_000)
    adjacent_second = copy.deepcopy(first)
    adjacent_second["codex_resets_at"] += 1
    news_gold = {
        "news": {"mode": "digest", "text": "sample", "generated_at": 1_790_238_500},
        "gold": {"price_gram_cny": 951.8, "quote_time": 1_790_238_500},
    }

    assert signals.activity_key(first) == signals.activity_key(adjacent_second)
    assert signals.ai_visual_key(first) == signals.ai_visual_key(adjacent_second)
    assert signals.news_gold_visual_key(first, news_gold) == signals.news_gold_visual_key(
        adjacent_second, news_gold
    )

    real_window_change = copy.deepcopy(first)
    real_window_change["codex_resets_at"] += 61
    assert signals.activity_key(first) != signals.activity_key(real_window_change)


def test_token_text_formats_keep_zero_and_unknown_distinct():
    assert preview.fmt_compact_tokens(0) == "0"
    assert preview.fmt_compact_tokens(999) == "999"
    assert preview.fmt_compact_tokens(1000) == "1.00k"
    assert preview.fmt_compact_tokens(10_000) == "10.0k"
    assert preview.fmt_compact_tokens(100_000) == "100k"
    assert preview.fmt_compact_tokens(1_000_000) == "1.00M"
    assert preview.fmt_full_tokens(999_999_999) == "999999999"
    assert preview.fmt_full_tokens(0) == "0"
    assert preview.fmt_full_tokens(None) == "--"
    assert preview.fmt_full_tokens(12840, complete=False) == "12840+"


def test_firmware_pages_select_the_intended_formatter():
    source = (Path(__file__).resolve().parents[2] / "firmware/src/panel_pages.cpp").read_text(
        encoding="utf-8"
    )
    assert "fmtCompactTodayTokens(ai, tokenTxt, sizeof(tokenTxt));" in source
    assert "fmtTodayTokens(ai, todayTxt, sizeof(todayTxt));" in source
