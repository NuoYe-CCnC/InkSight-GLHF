#!/usr/bin/env python3
"""Optional host-side sync loop; no remote sources are enabled implicitly."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import cloud_publish
from runtime_config import from_operator_secrets

CODEX_POLL_SECONDS = 30 * 60


def _admin_token() -> str:
    env_file = Path(__file__).resolve().parents[1] / "backend" / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("ADMIN_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"\'')
    except OSError:
        pass
    return ""


def cycle(mac: str, *, codex: bool = False, request_queues: bool = False,
          heartbeat: bool = False, backend: str = "http://127.0.0.1:8080") -> None:
    config = from_operator_secrets(mac, backend=backend)
    token = _admin_token()
    if not token:
        raise RuntimeError("set ADMIN_TOKEN in shared/backend/.env before host sync")
    env = {**os.environ, "ADMIN_TOKEN": token}
    config["admin_token"] = token
    if codex:
        # Read the local Codex quota before composing the screen. This does not
        # invoke a model or spend a reset opportunity. A hung CLI must not
        # stall the publisher indefinitely.
        try:
            subprocess.run([sys.executable, str(Path(__file__).with_name("codex_quota_probe.py")),
                            "--server", config["backend"], "--mac", "MAC-CODEX", "--source", "mac"],
                           env=env, check=False, timeout=65,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            pass
    cloud_publish.run(config, once=True, force=heartbeat)
    if request_queues:
        for script in ("gold_requests.py", "news_requests.py"):
            subprocess.run([sys.executable, str(Path(__file__).with_name(script)),
                            "--operator-mac", mac], env=env, check=False)


def _watch_parent(parent_pid: int) -> None:
    while True:
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGTERM)
            return
        time.sleep(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mac", required=True, help="target ESP32 MAC; never a private account ID")
    ap.add_argument("--codex", action="store_true", help="enable local Codex quota probe")
    ap.add_argument("--request-queues", action="store_true",
                    help="handle device gold/news requests; may call configured paid providers")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--backend", default="http://127.0.0.1:8080",
                    help="local backend URL, for example the desktop app's selected port")
    ap.add_argument("--probe-webdav", action="store_true",
                    help="explicitly test WebDAV PUT/GET/DELETE without model calls")
    ap.add_argument("--parent-pid", type=int,
                    help="desktop launcher PID; stop the publish loop if it exits")
    ap.add_argument("--interval", type=int, default=60, help="loop period, minimum 60 seconds")
    args = ap.parse_args()
    if args.interval < 60:
        ap.error("--interval must be at least 60")
    if args.parent_pid is not None:
        if args.parent_pid < 2 or os.getppid() != args.parent_pid:
            ap.error("desktop parent is no longer running")
        threading.Thread(target=_watch_parent, args=(args.parent_pid,), daemon=True).start()
    if args.probe_webdav:
        config = from_operator_secrets(args.mac, backend=args.backend)
        raise SystemExit(0 if cloud_publish.probe_webdav(
            config["webdav"], config["user"], config["password"]) else 1)
    last_heartbeat = 0
    next_codex_at = 0
    while True:
        now = int(time.time())
        heartbeat = now - last_heartbeat >= 600
        read_codex = bool(args.codex and now >= next_codex_at)
        cycle(args.mac, codex=read_codex, request_queues=args.request_queues,
              heartbeat=heartbeat, backend=args.backend)
        if read_codex:
            next_codex_at = now + CODEX_POLL_SECONDS
        if heartbeat:
            last_heartbeat = now
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
