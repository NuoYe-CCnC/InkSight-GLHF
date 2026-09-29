"""CLI discovery must work for a LaunchAgent without trusting arbitrary PATH entries."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PROBE_PATH = Path(__file__).resolve().parents[2] / "tools" / "codex_quota_probe.py"


@pytest.fixture
def probe_module():
    spec = importlib.util.spec_from_file_location("inksight_codex_quota_probe_test", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_macos_candidates_include_current_bundle_before_legacy(probe_module):
    paths = probe_module._default_codex_candidates("darwin")
    assert paths[0].endswith("/codex-cli/CodexCLI.app/Contents/MacOS/codex")
    assert paths[1] == "/Applications/ChatGPT.app/Contents/Resources/codex"


def test_current_bundle_is_found_with_launchagent_path(probe_module, monkeypatch, tmp_path):
    monkeypatch.delenv("INKSIGHT_CODEX_CLI", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    modern = _make_executable(
        tmp_path / "ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
    )
    legacy = _make_executable(tmp_path / "ChatGPT.app/Contents/Resources/codex")
    assert probe_module._find_codex((str(modern), str(legacy))) == str(modern)


def test_legacy_fallback_skips_nonexecutable_current_bundle(probe_module, tmp_path):
    modern = tmp_path / "codex-cli/CodexCLI.app/Contents/MacOS/codex"
    modern.parent.mkdir(parents=True)
    modern.write_text("not executable", encoding="utf-8")
    legacy = _make_executable(tmp_path / "codex")
    assert probe_module._find_codex((str(modern), str(legacy))) == str(legacy)


def test_arbitrary_path_match_is_ignored(probe_module, monkeypatch, tmp_path):
    untrusted = tmp_path / "untrusted"
    _make_executable(untrusted / "codex")
    monkeypatch.setenv("PATH", str(untrusted))
    monkeypatch.delenv("INKSIGHT_CODEX_CLI", raising=False)
    assert probe_module._find_codex((str(tmp_path / "missing"),)) is None


def test_explicit_override_requires_absolute_executable(probe_module, monkeypatch, tmp_path):
    installed = _make_executable(tmp_path / "custom-install/codex")
    monkeypatch.setenv("INKSIGHT_CODEX_CLI", str(installed))
    assert probe_module._find_codex() == str(installed)
    monkeypatch.setenv("INKSIGHT_CODEX_CLI", "relative/codex")
    assert probe_module._find_codex() is None
    assert "绝对路径" in probe_module.probe()["error"]


def test_executable_disappearing_before_start_has_clear_error(probe_module, monkeypatch, tmp_path):
    monkeypatch.setattr(probe_module, "_find_codex", lambda: str(tmp_path / "codex"))

    def missing_executable(*_args, **_kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(probe_module.subprocess, "Popen", missing_executable)
    result = probe_module.probe()
    assert result == {"ok": False, "error": "Codex CLI 无法启动（FileNotFoundError）"}


def test_7d_window_survives_multi_bucket_response(probe_module):
    normalized = probe_module.normalize({
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {"windowDurationMins": 300, "usedPercent": 12, "resetsAt": 1},
                "secondary": {"windowDurationMins": 10080, "usedPercent": 34, "resetsAt": 2},
                "planType": "plus",
            }
        }
    })
    assert [item["duration_minutes"] for item in normalized["windows"]] == [300, 10080]
    assert normalized["plan"] == "plus"
    assert probe_module.has_seven_day_window(normalized)


def test_30d_only_response_is_not_mislabeled_7d(probe_module):
    normalized = probe_module.normalize({
        "rateLimits": {
            "primary": {"windowDurationMins": 43200, "usedPercent": 4, "resetsAt": 1}
        }
    })
    assert [item["duration_minutes"] for item in normalized["windows"]] == [43200]
