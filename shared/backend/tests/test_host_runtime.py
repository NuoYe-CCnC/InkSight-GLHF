from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))
import runtime_config  # noqa: E402
import host_cycle  # noqa: E402


def test_operator_runtime_uses_existing_secrets_file(tmp_path):
    source = tmp_path / "secrets.json"
    source.write_text(json.dumps({"cloud": {
        "base_url": "https://example.invalid/dav", "user": "example", "password": "private"
    }}), encoding="utf-8")
    source.chmod(0o600)
    result = runtime_config.from_operator_secrets("aa:bb:cc:dd:ee:ff", path=source)
    assert result["devices"] == [{"mac": "AABBCCDDEEFF", "modes": ["AI_USAGE"]}]
    with pytest.raises(ValueError):
        runtime_config.from_operator_secrets("not-a-mac", path=source)
    with pytest.raises(ValueError):
        runtime_config.from_operator_secrets("aa:bb:cc:dd:ee:ff", path=source,
                                             backend="https://remote.invalid")


def test_host_cycle_does_not_enable_paid_or_queue_sources_by_default(monkeypatch):
    monkeypatch.setattr(host_cycle, "from_operator_secrets", lambda _mac, **_kw: {
        "backend": "http://127.0.0.1:8080", "webdav": "https://example.invalid/dav",
        "devices": [{"mac": "AABBCCDDEEFF", "modes": ["AI_USAGE"]}],
    })
    monkeypatch.setattr(host_cycle, "_admin_token", lambda: "unit-test-token")
    calls = []
    monkeypatch.setattr(host_cycle.cloud_publish, "run", lambda cfg, **kw: calls.append((cfg, kw)))
    monkeypatch.setattr(host_cycle.subprocess, "run", lambda *_a, **_k: pytest.fail("unexpected child"))
    host_cycle.cycle("AABBCCDDEEFF")
    assert len(calls) == 1
    assert calls[0][0]["admin_token"] == "unit-test-token"


def test_host_cycle_uses_selected_loopback_port(monkeypatch):
    seen = []
    monkeypatch.setattr(host_cycle, "from_operator_secrets", lambda _mac, **kw: (
        seen.append(kw["backend"]) or {"backend": kw["backend"],
        "webdav": "https://example.invalid/dav", "devices": []}))
    monkeypatch.setattr(host_cycle, "_admin_token", lambda: "unit-test-token")
    monkeypatch.setattr(host_cycle.cloud_publish, "run", lambda *_a, **_kw: None)
    host_cycle.cycle("AABBCCDDEEFF", backend="http://127.0.0.1:18137")
    assert seen == ["http://127.0.0.1:18137"]


def test_host_cycle_watchdog_stops_orphaned_desktop_loop(monkeypatch):
    signals = []
    monkeypatch.setattr(host_cycle.os, "getppid", lambda: 1)
    monkeypatch.setattr(host_cycle.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    host_cycle._watch_parent(12345)
    assert len(signals) == 1
    assert signals[0][0] == host_cycle.os.getpid()


def test_runtime_files_are_in_source_candidate_allowlist():
    root = Path(__file__).resolve().parents[3]
    script = root / "shared/tools/release/build_candidate.py"
    spec = importlib.util.spec_from_file_location("runtime_release_builder", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    allowed = {path.relative_to(root).as_posix() for path in module.collect(root)}
    assert {"docs/HOST_RUNTIME.zh-CN.md", "shared/tools/host_cycle.py",
            "shared/tools/runtime_config.py", "shared/tools/codex_quota_probe.py",
            "shared/tools/cloud_publish.py", "shared/tools/news_requests.py",
            "shared/tools/gold_requests.py"} <= allowed
