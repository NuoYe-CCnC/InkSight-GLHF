#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Process authenticated WebDAV cold-start recovery requests for XAUS.

设备仅在真实冷启动且已发布报价缺失或超过 24 小时时写 requests/<MAC>.json。
本工具每发布周期运行：
  1) 逐个配置设备 GET requests/<MAC>.json（404=无请求）；
  2) 有请求 → 调后端 POST /api/admin/gold/catchup（后端再次校验年龄、1h 冷却与日上限）；
  3) 后端确认数据已满足(noop)或补拉成功(fetched) → 删除远端请求文件；
     blocked/failed/auth/pending → 保留文件待下周期（受后端冷却约束，不无限重试）。
鉴权：ADMIN_TOKEN 从 backend/.env 读取（不经命令行/日志）。
"""
import argparse
import base64
import json
import hashlib
import os
import sys
import urllib.error
import urllib.request

REQ_DIR = "requests"


def load_cfg(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _admin_token(backend_env):
    try:
        for line in open(backend_env, encoding="utf-8"):
            line = line.strip()
            if line.startswith("ADMIN_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _webdav_auth(cfg):
    user, pw = cfg.get("user", ""), cfg.get("password", "")
    hdr = {}
    if user:
        hdr["Authorization"] = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
    return hdr


def _webdav_get(url, hdr):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={**hdr, "User-Agent": "inksight/1.0"}), timeout=20) as r:
            return r.status, r.read(), r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        return e.code, b"", None


def _webdav_delete(url, hdr, etag=None):
    try:
        headers = {**hdr, "User-Agent": "inksight/1.0"}
        if etag:
            headers["If-Match"] = etag
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers, method="DELETE"), timeout=20) as r:
            return r.status in (200, 204)
    except urllib.error.HTTPError as e:
        return e.code in (200, 204, 404)


def _ensure_req_dir(webdav_base: str, hdr) -> bool:
    """确保 requests/ 集合存在：设备 PUT 请求文件前父目录必须已建（否则 409）。
    已有 → MKCOL 返回 405，视为成功。"""
    url = f"{webdav_base}/{REQ_DIR}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={**hdr, "User-Agent": "inksight/1.0"}, method="MKCOL"), timeout=20) as r:
            return r.status in (200, 201, 204)
    except urllib.error.HTTPError as e:
        return e.code in (200, 201, 204, 405)  # 405=已存在
    except Exception:  # noqa: BLE001
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="shared/tools/cloud_publish.json")
    ap.add_argument("--operator-mac", help="使用统一私密配置中的 WebDAV 凭据")
    ap.add_argument("--backend", default=None)
    args = ap.parse_args()

    if args.operator_mac:
        from runtime_config import from_operator_secrets
        cfg = from_operator_secrets(args.operator_mac, backend=args.backend or "http://127.0.0.1:8080")
    else:
        cfg = load_cfg(args.config)
    base = (args.backend or cfg.get("backend") or "").rstrip("/")
    webdav = (cfg.get("webdav") or "").rstrip("/")
    env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "backend", ".env")
    token = _admin_token(env_file)
    if not token:
        print("[goldreq] ADMIN_TOKEN 缺失，跳过")
        return 1
    devices = [d.get("mac", "").upper().replace(":", "") for d in cfg.get("devices", []) if d.get("mac")]
    if not devices:
        print("[goldreq] 无配置设备")
        return 0

    hdr = _webdav_auth(cfg)
    if webdav:
        _ensure_req_dir(webdav, hdr)
    found = []
    for mac in devices:
        url = f"{webdav}/{REQ_DIR}/{mac}.json"
        st, body, etag = _webdav_get(url, hdr)
        if st == 200:
            try:
                req = json.loads(body.decode("utf-8", "ignore"))
            except Exception:
                req = None
            rid = str((req or {}).get("request_id") or "")
            if (isinstance(req, dict) and req.get("type") == "gold_refresh"
                    and 8 <= len(rid) <= 96):
                found.append((mac, req, etag))
            else:
                print("[goldreq] 请求文件格式异常，忽略并删除")
                _webdav_delete(url, hdr, etag)
        elif st == 404:
            pass
        else:
            print(f"[goldreq] 请求队列查询 HTTP {st}")
    if not found:
        return 0
    print(f"[goldreq] 发现 {len(found)} 个设备唤醒刷新请求")
    api = f"{base}/api/admin/gold/catchup"
    handled = 0
    seen = set()
    for mac, item, etag in found:
        rid = str(item["request_id"])
        if rid in seen:
            _webdav_delete(f"{webdav}/{REQ_DIR}/{mac}.json", hdr, etag)
            handled += 1
            continue
        seen.add(rid)
        fingerprint = hashlib.sha256(rid.encode()).hexdigest()[:12]
        body = json.dumps({"request_id": rid, "reason": "publisher_queue"}).encode()
        try:
            req = urllib.request.Request(api, data=body, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=60) as response:
                result = json.loads(response.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as exc:
            print(f"[goldreq] 后端 HTTP {exc.code} request={fingerprint}（保留）")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"[goldreq] 后端不可达 {type(exc).__name__}（保留）")
            continue
        # The backend has durably handled this logical request, including a
        # bounded provider failure. A later wake creates a new request id.
        deleted = _webdav_delete(f"{webdav}/{REQ_DIR}/{mac}.json", hdr, etag)
        if not deleted:
            print(f"[goldreq] request={fingerprint} queue changed; newer request retained")
        handled += 1
        print(f"[goldreq] request={fingerprint} action={result.get('action')}")
    print(f"[goldreq] 已处理 {handled}/{len(found)} 个请求")
    return 0 if handled == len(found) else 1


if __name__ == "__main__":
    sys.exit(main())
