#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
codex_quota_probe.py — Codex 订阅额度探测（跨平台：macOS / Windows / Linux）

数据源：本机 Codex CLI 的 `codex app-server`（stdio JSON-RPC）。
前提：本机已安装 Codex CLI 并完成 ChatGPT 登录（订阅模式）。
接口返回字段可能随 Codex 版本变化；只接受可验证的 7 日窗口，其他响应不覆盖旧值。
协议说明：https://learn.chatgpt.com/docs/app-server

用法（在你自己的 Mac / Windows 上运行）：
  python codex_quota_probe.py                 # 一次性探测并打印
  python codex_quota_probe.py --json          # 输出 §2.2 codex-usage payload（可接后端）
  python codex_quota_probe.py --interval 1800 --server http://127.0.0.1:8080 --source mac
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from core import codex_collector_health as health  # noqa: E402

METHOD = "account/rateLimits/read"  # 返回 windows: [{durationMinutes, usedPercent, resetsAt}]

def jsonrpc(method: str, params: dict | None = None, ident: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}

def rpc_read(proc, msg: dict, timeout_sec: float = 15) -> dict | None:
    """向 codex app-server 写一条 JSON-RPC 请求，读到**对应 id** 的响应返回。

    跳过非 JSON 行与无关消息（codex 会推送 remoteControl/status/changed 等
    异步通知；新版协议 initialize 还要求 clientInfo 字段）。
    """
    try:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
    except Exception as e:  # noqa: BLE001
        print(f"[error] JSON-RPC 写入失败: {e}")
        return None
    want_id = msg.get("id")
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            return None
        try:
            m = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if want_id is None or m.get("id") == want_id:
            return m
        # 其它 id 的消息（旧响应/通知）：丢弃继续等
    return None

def _default_codex_candidates(platform_name: str | None = None) -> tuple[str, ...]:
    """Known installation locations; never execute an arbitrary PATH match."""
    platform_name = platform_name or sys.platform
    if platform_name == "darwin":
        resources = Path("/Applications/ChatGPT.app/Contents/Resources")
        return (
            str(resources / "codex-cli/CodexCLI.app/Contents/MacOS/codex"),
            str(resources / "codex"),  # Older ChatGPT desktop releases.
            "/opt/homebrew/bin/codex",
            "/usr/local/bin/codex",
            "/usr/bin/codex",
        )
    if platform_name == "win32":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        return (str(Path(program_files) / "OpenAI/Codex/codex.exe"),)
    return ("/usr/local/bin/codex", "/usr/bin/codex")


def _executable_path(candidate: str) -> str | None:
    """Accept an explicit absolute executable, including a known symlink."""
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if resolved.is_file() and os.access(resolved, os.X_OK):
        return str(resolved)
    return None


def _find_codex(candidates: tuple[str, ...] | None = None) -> str | None:
    """Use a configured absolute path or a known install location.

    LaunchAgents do not inherit an interactive shell's PATH.  Searching PATH
    can also select an unrelated writable directory's `codex` executable.
    """
    if candidates is None:
        configured = os.environ.get("INKSIGHT_CODEX_CLI", "").strip()
        if configured:
            return _executable_path(configured)
        candidates = _default_codex_candidates()
    for candidate in candidates:
        found = _executable_path(candidate)
        if found:
            return found
    return None

def probe(timeout_sec: int = 20) -> dict:
    """启动 codex app-server，走 initialize → initialized → account/rateLimits/read。"""
    codex_bin = _find_codex()
    if not codex_bin:
        detail = ("INKSIGHT_CODEX_CLI 必须指向可执行文件的绝对路径"
                  if os.environ.get("INKSIGHT_CODEX_CLI", "").strip()
                  else "找不到可执行 Codex CLI（检查 ChatGPT 安装或配置 INKSIGHT_CODEX_CLI）")
        return {"ok": False, "error": detail}
    cmd = [codex_bin, "app-server"]
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
    except OSError as exc:
        return {"ok": False, "error": f"Codex CLI 无法启动（{type(exc).__name__}）"}
    try:
        # 1) initialize (实验接口能力；新版(≥0.15x)要求 clientInfo 字段)
        init = jsonrpc("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "inksight-quota-probe", "version": "1.0"},
            "capabilities": {"experimentalApi": True},
        }, 1)
        r = rpc_read(proc, init)
        if r is None:
            return {"ok": False, "error": "app-server 无响应（常见于 Windows，见 https://cloud.tencent.com.cn/developer/article/2711795）"}
        if r.get("error"):
            return {"ok": False, "error": r["error"]}
        # 2) initialized 通知
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}}) + "\n")
        proc.stdin.flush()
        # 3) rateLimits
        r = rpc_read(proc, jsonrpc(METHOD, {}, 3))
        if r is None:
            return {"ok": False, "error": "rateLimits 无响应（可能未登录 / 版本变化）"}
        if r.get("error"):
            return {"ok": False, "error": r["error"]}
        return {"ok": True, "result": r.get("result", {})}
    finally:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass

def _fmt_dur(mins):
    if not mins:
        return "?"
    if mins % 1440 == 0:
        return f"{mins // 1440}D"
    if mins % 60 == 0:
        return f"{mins // 60}H"
    return f"{mins}m"

def normalize(result: dict) -> dict:
    """把 rateLimits 结果规整为 §2.2 codex-usage payload 的 windows 结构。

    新版(0.15x)结构：result.rateLimits.{primary,secondary}.{usedPercent,
    windowDurationMins,resetsAt} + planType + credits；兼容旧版 windows 列表。
    """
    windows = []
    legacy_rl = result.get("rateLimits") or {}
    by_limit = result.get("rateLimitsByLimitId") or {}
    rl = by_limit.get("codex") if isinstance(by_limit, dict) else None
    if not isinstance(rl, dict):
        rl = legacy_rl if isinstance(legacy_rl, dict) else {}

    def _win(x, prefer_key):
        if not isinstance(x, dict):
            return None
        dur = x.get(prefer_key) or x.get("windowDurationMins") or x.get("durationMinutes") or 0
        used = x.get("usedPercent")
        if used is None:
            return None
        return {
            "label": _fmt_dur(dur),
            "duration_minutes": dur,
            "used_percent": used,
            "resets_at": x.get("resetsAt"),
        }

    for key, dur_key in (("primary", "windowDurationMins"), ("secondary", "windowDurationMins")):
        w = _win(rl.get(key), dur_key)
        if w:
            windows.append(w)
    if not windows:  # 旧版 {windows:[...]} 或裸列表
        raw = result.get("windows") or []
        for w in raw:
            dur = w.get("durationMinutes") or w.get("windowDurationMins") or 0
            used = w.get("usedPercent")
            resets = w.get("resetsAt")
            windows.append({"label": _fmt_dur(dur), "duration_minutes": dur,
                            "used_percent": used, "resets_at": resets})
    reset_credits = result.get("rateLimitResetCredits") or {}
    available = reset_credits.get("availableCount")
    # 手动重置机会到期列表（2026-09-07 接入）：
    # 原始响应 rateLimitResetCredits.credits[] 每项含 {status, expiresAt(Unix秒), resetType…}。
    # 规则：仅 status=='available' 且 expiresAt>now（未过期）；升序；Unix 秒数组。
    # 接口无该结构 → reset_expiry_list=None（与空列表 [] 严格区分，缺失≠0 次）。
    exp_list = None
    raw_items = reset_credits.get("credits")
    now = int(time.time())
    total_raw = 0
    if isinstance(raw_items, list):
        total_raw = len(raw_items)
        exp_list = []
        for c in raw_items:
            if not isinstance(c, dict):
                continue
            st = c.get("status")
            exp = c.get("expiresAt")
            if st == "available" and isinstance(exp, int) and exp > now:
                exp_list.append(exp)
        exp_list = sorted(set(exp_list))
    out = {
        "windows": windows,
        "reset_credits_available": available,
        "reset_expiry_list": exp_list,
        "reset_expiry_total": total_raw,
        "ts": int(time.time()),
    }
    point_credits = rl.get("credits") if isinstance(rl.get("credits"), dict) else None
    if point_credits is not None:
        balance = point_credits.get("balance")
        if isinstance(balance, (str, int, float)) and not isinstance(balance, bool):
            out["credit_balance"] = str(balance)
        out["credit_has_credits"] = point_credits.get("hasCredits") is True
        out["credit_unlimited"] = point_credits.get("unlimited") is True
    account_id = result.get("accountId")
    if isinstance(account_id, str) and account_id:
        # Stable isolation key only; the raw account id never leaves this host.
        out["account_key"] = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:20]
    if available is not None and exp_list is not None and available != len(exp_list):
        # 次数与“可用且未过期”项数不一致 → 标记待核对（不猜测、不自动删除日期）
        out["reset_expiry_note"] = f"count/list mismatch: {available} vs {len(exp_list)} (raw {total_raw})"
    if isinstance(raw_items, list):
        out["reset_expiry_source"] = "api"
    if rl.get("planType"):
        out["plan"] = rl.get("planType")
    return out

def push(server: str, token: str, mac: str, source: str, payload: dict):
    import urllib.request
    body = json.dumps({"source": source, **payload}).encode()
    url = f"{server.rstrip('/')}/api/device/{mac}/external/codex-usage"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def _failure_reason(result: dict) -> str:
    error = result.get("error")
    detail = str(error)
    if "找不到可执行" in detail or "INKSIGHT_CODEX_CLI 必须" in detail:
        return "cli_missing"
    if "无法启动" in detail:
        return "cli_start"
    if "无响应" in detail or "写入失败" in detail:
        return "rpc_unavailable"
    return "rpc_error"


def has_seven_day_window(payload: dict) -> bool:
    return any(w.get("duration_minutes") == 7 * 1440
               for w in payload.get("windows", []) if isinstance(w, dict))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="只输出规整后的 payload JSON")
    ap.add_argument("--interval", type=int, help="定时模式间隔秒数（与 --server 配合）")
    ap.add_argument("--server", help="后端地址，如 http://127.0.0.1:8080")
    ap.add_argument("--token", default=os.environ.get("ADMIN_TOKEN", ""), help="推送 token；默认读取 ADMIN_TOKEN 环境变量")
    ap.add_argument("--mac", default="MAC-CODEX", help="推送到哪个设备名下（云端模式用伪设备名即可，默认 MAC-CODEX）")
    ap.add_argument("--source", default="mac", help="mac | windows")
    args = ap.parse_args()

    while True:
        managed = bool(args.server and args.token)
        if managed and not health.should_attempt():
            if not args.interval:
                return
            time.sleep(args.interval)
            continue
        try:
            r = probe()
        except Exception:  # noqa: BLE001
            r = {"ok": False, "error": "collector unexpected failure"}
        if not r.get("ok"):
            reason = _failure_reason(r)
            if managed:
                health.record(False, reason)
            print(json.dumps({"ok": False, "reason": reason}, ensure_ascii=False))
            if not args.interval:
                sys.exit(1)
            time.sleep(args.interval)
            continue
        payload = normalize(r["result"])
        if managed and not has_seven_day_window(payload):
            health.record(False, "missing_7d")
            print(json.dumps({"ok": False, "reason": "missing_7d"}, ensure_ascii=False))
            if not args.interval:
                sys.exit(1)
            time.sleep(args.interval)
            continue
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        elif args.server and args.token:
            try:
                st = push(args.server, args.token, args.mac, args.source, payload)
            except Exception:  # noqa: BLE001
                health.record(False, "push_failed")
                print(json.dumps({"ok": False, "reason": "push_failed"}, ensure_ascii=False))
                if not args.interval:
                    sys.exit(1)
                time.sleep(args.interval)
                continue
            health.record(True)
            print(f"[push] {st} 7D verified")
        else:
            print(json.dumps({"ok": True, **payload}, ensure_ascii=False))
        if not args.interval:
            break
        time.sleep(args.interval)

if __name__ == "__main__":
    main()
