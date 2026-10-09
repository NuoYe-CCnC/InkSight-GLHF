#!/usr/bin/env python3
"""Desktop-only loopback backend with a parent-process watchdog."""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))


def _watch_parent(parent_pid: int) -> None:
    if parent_pid < 2:
        raise ValueError("invalid desktop parent process")
    while True:
        if os.getppid() != parent_pid:
            from core.desktop_service import gate, status
            gate.pause()
            # A parent crash is not permission to kill a flash/paid worker.
            # Keep the loopback management endpoint until admitted work ends.
            while not status()['safe_to_stop']:
                threading.Event().wait(1.0)
            os.kill(os.getpid(), signal.SIGTERM)
            return
        threading.Event().wait(1.0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    if os.getppid() != args.parent_pid:
        parser.error("desktop parent is no longer running")
    os.environ["INKSIGHT_DESKTOP_MODE"] = "1"
    os.environ["INKSIGHT_DESKTOP_PORT"] = str(args.port)
    threading.Thread(target=_watch_parent, args=(args.parent_pid,), daemon=True).start()
    import uvicorn

    uvicorn.run("api.index:app", host="127.0.0.1", port=args.port, log_level="info",
                proxy_headers=False)


if __name__ == "__main__":
    main()
