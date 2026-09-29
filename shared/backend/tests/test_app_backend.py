from __future__ import annotations

import importlib.util
from pathlib import Path
import signal


SCRIPT = Path(__file__).resolve().parents[2] / "tools/app_backend.py"
SPEC = importlib.util.spec_from_file_location("desktop_backend_watchdog", SCRIPT)
assert SPEC and SPEC.loader
app_backend = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app_backend)


def test_backend_watchdog_stops_when_launcher_is_gone(monkeypatch):
    signals = []
    monkeypatch.setattr(app_backend.os, "getppid", lambda: 1)
    monkeypatch.setattr(app_backend.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    app_backend._watch_parent(12345)
    assert signals == [(app_backend.os.getpid(), signal.SIGTERM)]
