#!/usr/bin/env python3
"""Optional host-side sync loop; no remote sources are enabled implicitly."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

import cloud_publish
from runtime_config import from_operator_secrets


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
          heartbeat: bool = False) -> None:
    config = from_operator_secrets(mac)
    token = _admin_token()
    if not token:
        raise RuntimeError("set ADMIN_TOKEN in shared/backend/.env before host sync")
    env = {**os.environ, "ADMIN_TOKEN": token}
    config["admin_token"] = token
    cloud_publish.run(config, once=True, force=heartbeat)
    if codex:
        subprocess.run([sys.executable, str(Path(__file__).with_name("codex_quota_probe.py")),
                        "--server", config["backend"], "--mac", "MAC-CODEX", "--source", "mac"],
                       env=env, check=False)
    if request_queues:
        for script in ("gold_requests.py", "news_requests.py"):
            subprocess.run([sys.executable, str(Path(__file__).with_name(script)),
                            "--operator-mac", mac], env=env, check=False)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mac", required=True, help="target ESP32 MAC; never a private account ID")
    ap.add_argument("--codex", action="store_true", help="enable local Codex quota probe")
    ap.add_argument("--request-queues", action="store_true",
                    help="handle device gold/news requests; may call configured paid providers")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=60, help="loop period, minimum 60 seconds")
    args = ap.parse_args()
    if args.interval < 60:
        ap.error("--interval must be at least 60")
    last_heartbeat = 0
    while True:
        now = int(time.time())
        heartbeat = now - last_heartbeat >= 600
        cycle(args.mac, codex=args.codex, request_queues=args.request_queues,
              heartbeat=heartbeat)
        if heartbeat:
            last_heartbeat = now
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
