#!/usr/bin/env python3
"""Optional host-side sync loop; no remote sources are enabled implicitly."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.request

import cloud_publish
from runtime_config import from_operator_secrets

CODEX_POLL_SECONDS = 30 * 60
POLICY_FILE = Path(__file__).with_name("agent_policy.json")
_active_child: subprocess.Popen | None = None
_graceful_stop = False


def _request_stop(_signal, _frame):
    global _graceful_stop
    _graceful_stop = True


@dataclass(frozen=True)
class HostPolicy:
    publish_sec: int = 60
    codex_open_sec: int = CODEX_POLL_SECONDS
    codex_closed_sec: int = CODEX_POLL_SECONDS
    process_names: tuple[str, ...] = ("ChatGPT",)


def load_policy(path: Path = POLICY_FILE) -> HostPolicy:
    if not path.exists():
        return HostPolicy()
    value = json.loads(path.read_text(encoding="utf-8"))
    intervals = [value.get("publish_sec", 60), value.get("codex_open_sec", CODEX_POLL_SECONDS),
                 value.get("codex_closed_sec", CODEX_POLL_SECONDS)]
    if any(isinstance(n, bool) or not isinstance(n, int) or not 60 <= n <= 86400
           for n in intervals):
        raise ValueError("host policy intervals must be integers between 60 and 86400 seconds")
    names = value.get("chatgpt_process_names", ["ChatGPT"])
    if (not isinstance(names, list) or not 1 <= len(names) <= 8
            or any(not isinstance(n, str) or not n.strip() or len(n) > 80 or "\n" in n
                   for n in names)):
        raise ValueError("host policy process names are invalid")
    return HostPolicy(*intervals, tuple(names))


def codex_interval(policy: HostPolicy) -> int:
    for name in policy.process_names:
        result = subprocess.run(["/usr/bin/pgrep", "-x", name], check=False,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        if result.returncode == 0:
            return policy.codex_open_sec
    return policy.codex_closed_sec


def _stop_child() -> None:
    child = _active_child
    if child is not None and child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)


def _run_child(arguments: list[str], *, env: dict, timeout: int) -> None:
    global _active_child
    child = subprocess.Popen(arguments, env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    _active_child = child
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_child()
    finally:
        _active_child = None


def _shutdown(_signal, _frame) -> None:
    _stop_child()
    raise SystemExit(0)


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
        _run_child([sys.executable, str(Path(__file__).with_name("codex_quota_probe.py")),
                            "--server", config["backend"], "--mac", "MAC-CODEX", "--source", "mac"],
                   env=env, timeout=65)
    if request_queues:
        for script in ("gold_requests.py", "news_requests.py"):
            _run_child([sys.executable, str(Path(__file__).with_name(script)),
                        "--operator-mac", mac, "--backend", config["backend"]],
                       env=env, timeout=180)
    cloud_publish.run(config, once=True, force=heartbeat)


def recover_host(backend: str) -> dict:
    if not backend.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError("host recovery backend must be local loopback")
    token = _admin_token()
    if not token:
        raise RuntimeError("local admin token is missing")
    request = urllib.request.Request(
        backend.rstrip("/") + "/api/admin/host/recover", method="POST",
        data=json.dumps({"reason": "host-wake"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=120) as response:
        value = json.loads(response.read())
    return {"ok": bool(value.get("ok")), "deduped": bool(value.get("deduped"))}


def _watch_parent(parent_pid: int) -> None:
    while True:
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGUSR1)
            return
        time.sleep(1)


def main() -> None:
    global _graceful_stop
    _graceful_stop = False
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mac", help="target ESP32 MAC; never a private account ID")
    ap.add_argument("--codex", action="store_true", help="enable local Codex quota probe")
    ap.add_argument("--request-queues", action="store_true",
                    help="handle device gold/news requests; may call configured paid providers")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--backend", default="http://127.0.0.1:8080",
                    help="local backend URL, for example the desktop app's selected port")
    ap.add_argument("--probe-webdav", action="store_true",
                    help="explicitly test WebDAV PUT/GET/DELETE without model calls")
    ap.add_argument("--recover-host", action="store_true",
                    help="explicitly invoke the existing bounded host-wake recovery")
    ap.add_argument("--parent-pid", type=int,
                    help="desktop launcher PID; stop the publish loop if it exits")
    ap.add_argument("--interval", type=int, help="override policy loop period, minimum 60 seconds")
    args = ap.parse_args()
    policy = load_policy()
    interval = args.interval if args.interval is not None else policy.publish_sec
    if interval < 60:
        ap.error("--interval must be at least 60")
    signal.signal(signal.SIGUSR1, _request_stop)
    if args.parent_pid is not None:
        if args.parent_pid < 2 or os.getppid() != args.parent_pid:
            ap.error("desktop parent is no longer running")
        threading.Thread(target=_watch_parent, args=(args.parent_pid,), daemon=True).start()
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    if args.recover_host:
        text = recover_host(args.backend)
        print(json.dumps(text))
        return
    if not args.mac:
        ap.error("--mac is required for cloud publishing or probing")
    if args.probe_webdav:
        config = from_operator_secrets(args.mac, backend=args.backend)
        raise SystemExit(0 if cloud_publish.probe_webdav(
            config["webdav"], config["user"], config["password"]) else 1)
    last_heartbeat = 0
    last_codex_at = float("-inf")
    while not _graceful_stop:
        now = time.monotonic()
        heartbeat = now - last_heartbeat >= 600
        read_codex = bool(args.codex and now - last_codex_at >= codex_interval(policy))
        try:
            cycle(args.mac, codex=read_codex, request_queues=args.request_queues,
                  heartbeat=heartbeat, backend=args.backend)
        except Exception as exc:
            # An unavailable network/backend must not permanently end the app's
            # publish loop or leak credentials through an exception message.
            print("host cycle failed: " + type(exc).__name__, flush=True)
        if read_codex:
            last_codex_at = now
        if heartbeat:
            last_heartbeat = now
        if args.once:
            return
        # A graceful request finishes the current cycle including its paid
        # queue child and atomic cloud write; idle exit latency is <= 1 second.
        for _ in range(interval):
            if _graceful_stop:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
