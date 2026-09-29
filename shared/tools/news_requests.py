#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Process authenticated WebDAV requests for the currently due news issue.

The device only reports that its feed is behind. The backend independently
selects today's latest due issue and applies the existing retry window and
per-issue model-call ledger. Replays are idempotent and cannot backfill history.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

REQ_DIR = "news-requests"


def load_cfg(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _admin_token(backend_env):
    try:
        with open(backend_env, encoding="utf-8") as handle:
            for line in handle:
                if line.strip().startswith("ADMIN_TOKEN="):
                    return line.strip().split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _auth(cfg):
    user = cfg.get("user", "")
    if not user:
        return {}
    raw = f"{user}:{cfg.get('password', '')}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def _get(url, headers):
    try:
        req = urllib.request.Request(url, headers={**headers, "User-Agent": "inksight/1.0"})
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, response.read(), response.headers.get("ETag")
    except urllib.error.HTTPError as exc:
        return exc.code, b"", None


def _delete(url, headers, etag=None):
    merged = {**headers, "User-Agent": "inksight/1.0"}
    if etag:
        merged["If-Match"] = etag
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=merged, method="DELETE"), timeout=20) as response:
            return response.status in (200, 204)
    except urllib.error.HTTPError as exc:
        return exc.code in (200, 204, 404)


def _ensure_dir(base, headers):
    try:
        req = urllib.request.Request(f"{base}/{REQ_DIR}", headers={**headers, "User-Agent": "inksight/1.0"},
                                     method="MKCOL")
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status in (200, 201, 204)
    except urllib.error.HTTPError as exc:
        return exc.code in (200, 201, 204, 405)
    except Exception:  # noqa: BLE001
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="shared/tools/cloud_publish.json")
    parser.add_argument("--operator-mac", help="使用统一私密配置中的 WebDAV 凭据")
    parser.add_argument("--backend", default=None)
    args = parser.parse_args()
    if args.operator_mac:
        from runtime_config import from_operator_secrets
        cfg = from_operator_secrets(args.operator_mac, backend=args.backend or "http://127.0.0.1:8080")
    else:
        cfg = load_cfg(args.config)
    backend = (args.backend or cfg.get("backend") or "").rstrip("/")
    webdav = (cfg.get("webdav") or "").rstrip("/")
    env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "backend", ".env")
    token = _admin_token(env_file)
    if not token:
        print("[newsreq] ADMIN_TOKEN 缺失，跳过")
        return 1
    devices = [str(row.get("mac") or "").upper().replace(":", "")
               for row in cfg.get("devices", []) if row.get("mac")]
    if not devices:
        print("[newsreq] 无配置设备")
        return 0
    headers = _auth(cfg)
    if webdav:
        _ensure_dir(webdav, headers)
    found = []
    for mac in devices:
        url = f"{webdav}/{REQ_DIR}/{mac}.json"
        status, raw, etag = _get(url, headers)
        if status == 404:
            continue
        if status != 200:
            print(f"[newsreq] 请求队列查询 HTTP {status}")
            continue
        try:
            payload = json.loads(raw.decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            payload = None
        request_id = str((payload or {}).get("request_id") or "")
        valid = (isinstance(payload, dict) and payload.get("type") == "news_due_check"
                 and request_id.startswith("news-") and 15 <= len(request_id) <= 64)
        if not valid:
            print("[newsreq] 请求文件格式异常，忽略并删除")
            _delete(url, headers, etag)
            continue
        found.append((mac, request_id, etag))
    if not found:
        return 0
    print(f"[newsreq] 发现 {len(found)} 个资讯到期检查请求")
    seen = set()
    handled = 0
    for mac, request_id, etag in found:
        url = f"{webdav}/{REQ_DIR}/{mac}.json"
        if request_id in seen:
            handled += int(_delete(url, headers, etag))
            continue
        seen.add(request_id)
        fingerprint = hashlib.sha256(request_id.encode()).hexdigest()[:12]
        body = json.dumps({"request_id": request_id, "reason": "publisher-queue"}).encode()
        request = urllib.request.Request(
            f"{backend}/api/admin/news/check", data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                result = json.loads(response.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as exc:
            print(f"[newsreq] 后端 HTTP {exc.code} request={fingerprint}（保留）")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"[newsreq] 后端不可达 {type(exc).__name__}（保留）")
            continue
        deleted = _delete(url, headers, etag)
        if not deleted:
            print(f"[newsreq] request={fingerprint} queue changed; newer request retained")
        handled += 1
        print(f"[newsreq] request={fingerprint} action={result.get('action')}")
    print(f"[newsreq] 已处理 {handled}/{len(found)} 个请求")
    return 0 if handled == len(found) else 1


if __name__ == "__main__":
    sys.exit(main())
