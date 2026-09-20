"""Offline tests for the daily-message provider contract and rights gate."""
from __future__ import annotations

import pytest

from core import daily_message_provider as provider


def test_all_researched_providers_are_release_blocked_by_default():
    matrix = provider.provider_matrix()
    assert matrix
    assert all(row["release_eligible"] is False for row in matrix.values())
    assert all(row["cache_allowed"] is False for row in matrix.values())


@pytest.mark.parametrize("provider_id", sorted(provider.PROVIDERS))
def test_rights_gate_stops_network_before_opener(provider_id):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("network opener must not be reached")

    with pytest.raises(provider.ContentRightsUnverified):
        provider.fetch_approved(provider_id, opener=forbidden)
    assert calls == []


def test_normalized_contract_keeps_provenance_and_does_not_invent_author():
    item = provider.normalize_payload(
        "affirmations", {"affirmation": "A short test sentence"}, fetched_at=123)
    assert item == {
        "id": item["id"], "text": "A short test sentence", "lang": "en",
        "source": "affirmations", "source_url": "https://www.affirmations.dev",
        "author": None, "content_license": "unverified",
        "license_evidence_url": "https://github.com/annthurium/affirmations",
        "fetched_at": 123, "release_eligible": False, "cache_allowed": False,
    }


def test_english_source_cannot_reach_chinese_panel_or_cache():
    item = provider.normalize_payload(
        "zenquotes", [{"q": "A short quotation", "a": "Example Author"}], fetched_at=1)
    assert provider.panel_eligible(item) == (False, "content-rights-unverified")
    assert provider.cache_eligible(item) is False


@pytest.mark.parametrize("text", ["<b>指令</b>", "第一行\n第二行", "正常\x00异常", "字" * 97])
def test_markup_controls_multiline_and_overlength_are_rejected_without_truncation(text):
    with pytest.raises(provider.ProviderError):
        provider.normalize_payload("hitokoto", {"hitokoto": text}, fetched_at=1)


def test_retry_policy_is_bounded_and_has_backoff():
    assert provider.retry_delays() == (0, 60, 300)
