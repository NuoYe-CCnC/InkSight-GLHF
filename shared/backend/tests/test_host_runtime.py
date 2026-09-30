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


def test_codex_read_precedes_publish_without_paid_queues(monkeypatch):
    events = []
    monkeypatch.setattr(host_cycle, "from_operator_secrets", lambda _mac, **_kw: {
        "backend": "http://127.0.0.1:18137", "webdav": "https://example.invalid/dav",
        "devices": [{"mac": "AABBCCDDEEFF", "modes": ["AI_USAGE"]}],
    })
    monkeypatch.setattr(host_cycle, "_admin_token", lambda: "unit-test-token")
    monkeypatch.setattr(host_cycle, "_run_child", lambda args, **kwargs: (
        events.append(("codex", args, kwargs))
    ))
    monkeypatch.setattr(host_cycle.cloud_publish, "run", lambda *_a, **_kw: events.append(("publish",)))
    host_cycle.cycle("AABBCCDDEEFF", codex=True)
    assert [event[0] for event in events] == ["codex", "publish"]
    assert "codex_quota_probe.py" in events[0][1][1]
    assert events[0][2]["timeout"] == 65
    assert "--request-queues" not in events[0][1]
    swift = (Path(__file__).resolve().parents[2] / "tools/app/macos/InkSightApp.swift").read_text()
    assert '"--backend", baseURL.absoluteString, "--codex"' in swift


def test_paid_queue_opt_in_uses_app_port_and_finishes_before_publication(monkeypatch):
    events = []
    selected = "http://127.0.0.1:18137"
    monkeypatch.setattr(host_cycle, "from_operator_secrets", lambda _mac, **kw: {
        "backend": kw["backend"], "webdav": "https://example.invalid/dav", "devices": []})
    monkeypatch.setattr(host_cycle, "_admin_token", lambda: "unit-test-token")
    monkeypatch.setattr(host_cycle, "_run_child", lambda args, **kw: events.append(args))
    monkeypatch.setattr(host_cycle.cloud_publish, "run", lambda *_a, **_kw: events.append("publish"))
    host_cycle.cycle("AABBCCDDEEFF", request_queues=True, backend=selected)
    assert [Path(event[1]).name for event in events[:2]] == ["gold_requests.py", "news_requests.py"]
    assert all(event[event.index("--backend") + 1] == selected for event in events[:2])
    assert events[-1] == "publish"


def test_migrated_agent_policy_preserves_dynamic_codex_cadence(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({"publish_sec": 120, "codex_open_sec": 60,
        "codex_closed_sec": 600, "chatgpt_process_names": ["ChatGPT"]}))
    policy = host_cycle.load_policy(policy_path)
    assert policy.publish_sec == 120
    monkeypatch.setattr(host_cycle.subprocess, "run", lambda *_a, **_kw: type("Result", (), {"returncode": 0})())
    assert host_cycle.codex_interval(policy) == 60
    monkeypatch.setattr(host_cycle.subprocess, "run", lambda *_a, **_kw: type("Result", (), {"returncode": 1})())
    assert host_cycle.codex_interval(policy) == 600
    policy_path.write_text('{"publish_sec": false}')
    with pytest.raises(ValueError):
        host_cycle.load_policy(policy_path)


def test_network_failure_does_not_end_the_long_running_publisher(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["host_cycle.py", "--mac", "AABBCCDDEEFF"])
    monkeypatch.setattr(host_cycle, "load_policy", lambda: host_cycle.HostPolicy())
    monkeypatch.setattr(host_cycle.signal, "signal", lambda *_args: None)
    calls = []
    def cycle(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("private url must not be printed")
        raise SystemExit(0)
    monkeypatch.setattr(host_cycle, "cycle", cycle)
    monkeypatch.setattr(host_cycle.time, "sleep", lambda _seconds: None)
    with pytest.raises(SystemExit):
        host_cycle.main()
    assert len(calls) == 2


def test_host_recovery_is_loopback_only_and_uses_existing_admin_gate(monkeypatch):
    seen = []
    monkeypatch.setattr(host_cycle, "_admin_token", lambda: "unit-test-token")
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self): return b'{"ok":true,"deduped":true,"private":"omit"}'
    monkeypatch.setattr(host_cycle.urllib.request, "urlopen", lambda request, **kw: (
        seen.append(request) or Response()))
    assert host_cycle.recover_host("http://127.0.0.1:18137") == {"ok": True, "deduped": True}
    assert seen[0].full_url == "http://127.0.0.1:18137/api/admin/host/recover"
    assert json.loads(seen[0].data) == {"reason": "host-wake"}
    with pytest.raises(ValueError):
        host_cycle.recover_host("https://remote.invalid")


def test_timed_out_child_is_reaped_and_not_left_running():
    host_cycle._run_child([sys.executable, "-c", "import time; time.sleep(30)"],
                          env={}, timeout=0.05)
    assert host_cycle._active_child is None


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
