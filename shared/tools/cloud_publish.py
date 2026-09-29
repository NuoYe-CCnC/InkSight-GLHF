#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cloud_publish.py — 发布端：把后端内容合并为"同屏仪表盘"上传坚果云 WebDAV

形态（北京时间）：
  发布端持续提供版本化双页数据与官方工作日日历；设备端第三阶段调度器按
  活跃/轻度/夜间自适应检查并始终物理深睡，不再做分钟时钟刷新。
  发布端（Mac/Windows 谁在线谁跑）→ 生成各模式 payload → 合并为一张
  "DASHBOARD"（模块上下排布）→ PUT 到坚果云固定路径 <webdav>/<MAC>.json。
  设备（任意 WiFi）→ GET 该文件 → payload_id 未变则跳过刷屏。

前提：本机后端在运行（uvicorn，端口默认 8080）。
用法：
  python cloud_publish.py --config cloud_publish.json --once
  python cloud_publish.py --config cloud_publish.json            # 循环
  python cloud_publish.py --backend http://127.0.0.1:8080 \
      --webdav https://dav.jianguoyun.com/dav/inksight \
      --user you@example.com --pass <坚果云应用密码> \
      --mac AA:BB:CC:DD:EE:FF --modes AI_USAGE --once

config 示例 (cloud_publish.json)：
{
  "backend": "http://127.0.0.1:8080",
  "webdav": "https://dav.jianguoyun.com/dav/inksight",
  "user": "you@example.com",
  "password": "坚果云应用密码（账号→安全选项→添加应用密码）",
  "devices": [{"mac": "AA:BB:CC:DD:EE:FF", "modes": ["AI_USAGE"]}],
  "interval_sec": 300
}

常驻界面 = AI_USAGE（Codex 用量 + DeepSeek 额度）。热点模块（HOTLIST）能力已
具备但默认不启用：展示/推送规则待定，后续版本把 "HOTLIST" 加进某台设备的
modes 即可在同一屏出现热点卡片。
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import time
import tempfile
import urllib.error
import urllib.request

try:
    import fcntl  # type: ignore
except ImportError:  # Windows
    fcntl = None
    import msvcrt  # type: ignore

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cloud_publish_state.json")
STATE_LOCK_FILE = STATE_FILE + ".lock"


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_json(path, value):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_state(state):
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as handle:
                previous = json.load(handle)
            _atomic_json(STATE_FILE + ".bak", previous)
        except (OSError, json.JSONDecodeError):
            pass
    _atomic_json(STATE_FILE, state)


def fetch_payload(backend: str, mode: str, w=800, h=480, admin_token: str = ""):
    """取某模式的活数据（结构化）。带 admin_token → /api/admin/structured（云端模式，无设备上下文）。
    不带 → 旧 /api/render?fmt=structured（绑定设备/预览等场景，无设备时返回静态 fallback）。"""
    url = f"{backend.rstrip('/')}/api/render?w={w}&h={h}&fmt=structured&persona={mode}"
    headers = {}
    if admin_token:
        url = f"{backend.rstrip('/')}/api/admin/structured?w={w}&h={h}&persona={mode}"
        headers["Authorization"] = f"Bearer {admin_token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    if data.get("payload_type") != "screen":
        return None
    return data


def payload_id(payload):
    return payload.get("screen", {}).get("payload_id", "")


def publication_id(payload):
    """Transport version, including freshness observations but excluding heartbeat ts."""
    screen = payload.get("screen", {}) if isinstance(payload, dict) else {}
    return screen.get("publish_id") or screen.get("payload_id", "")


def _glyph_w(cp, size):
    return size * 3 // 4 if cp < 0x80 else size


def _utf8_width(s, size):
    return sum(_glyph_w(c, size) for c in s)


def _card_height(widgets, card_w):
    """按与设备端一致的度量估算卡片内容高度（CJK 16 行高 22、24 行高 30 等）。"""
    h = 6
    max_w = card_w - 32
    for w in widgets:
        t = w.get("type")
        if t == "text":
            size = 24 if "24" in str(w.get("font", "")) else 16
            line_h = size + 6
            text = str(w.get("text", ""))
            # 按字符宽估算断行行数（逐字符断行，同设备端）
            lines = 0
            cur = 0
            for ch in text:
                if ch in "\n\r":
                    lines += 1
                    cur = 0
                    continue
                gw = _glyph_w(ord(ch), size)
                if cur + gw > max_w and cur:
                    lines += 1
                    cur = 0
                cur += gw
            lines += 1
            ml = int(w.get("max_lines", 3)) or 1
            h += min(lines, ml) * line_h + 2
        elif t == "big_number":
            h += 83
        elif t == "progress":
            h += 42
        elif t == "separator":
            h += 8
        elif t == "spacer":
            h += int(w.get("height", 6)) + 2
        elif t == "icon_text":
            h += 34
        elif t == "forecast_row":
            h += 46
        else:
            h += 26
    return h


def _trim_text_tail(widgets, budget):
    """从尾部移除 text 行直到估算高度 ≤ budget（热点列表截断）。"""
    while _card_height(widgets, 800) > budget and widgets:
        removed = False
        for i in range(len(widgets) - 1, -1, -1):
            if widgets[i].get("type") == "text":
                ml = int(widgets[i].get("max_lines", 3))
                if ml > 1:
                    widgets[i]["max_lines"] = ml - 1
                    removed = True
                    break
                del widgets[i]
                removed = True
                break
        if not removed:
            break
    return widgets


def combine_dashboard(payloads: list, w=800, h=480):
    """多个模式 payload → 一张同屏仪表盘（全宽卡片，真实度量高度，放不下截断）。"""
    cards = []
    for p in payloads:
        if not p:
            continue
        ws = []
        mods = p.get("screen", {}).get("modules", [])
        for m in mods:
            ws.extend(m.get("widgets", []))
        if not ws:
            ws = p.get("screen", {}).get("widgets", [])
        if ws:
            cards.append(ws)
    if not cards:
        return None

    budget_total = h - 24
    # 先按比例分配：测量每卡原始高度，超出则优先截断后续卡
    measured = [_card_height(ws, w - 24) for ws in cards]
    total = sum(measured)
    scale = min(1.0, budget_total / max(total, 1))
    modules = []
    y_cursor = 10
    for idx, ws in enumerate(cards):
        budget = budget_total * scale if idx < len(cards) - 1 else budget_total - (y_cursor - 10)
        # 最后一卡用剩余预算
        if idx == len(cards) - 1:
            budget = budget_total - (y_cursor - 10)
        ch = _card_height(ws, w - 24)
        if ch > budget:
            ws = list(_trim_text_tail(list(ws), budget))
            ch = min(_card_height(ws, w - 24), budget)
        if ch < 30:
            break
        modules.append({
            "id": f"card_{idx}",
            "region": {"x": 12, "y": y_cursor, "w": w - 24, "h": ch},
            "widgets": ws,
        })
        y_cursor += ch + 10
        if y_cursor > h - 24:
            break
    if not modules:
        return None
    # 第二阶段元数据透传（取自首个结构化 payload；空则省略）
    meta = {}
    for p in payloads:
        scr = (p or {}).get("screen", {}) or {}
        for k in ("panels", "versions", "pages", "layout_version", "font_version",
                  "calendar", "display_preferences", "device_policy"):
            if k in scr and scr[k] not in (None, {}, []):
                meta[k] = scr[k]
        if meta:
            break
    raw = json.dumps([(m["id"], m.get("widgets", [])) for m in modules],
                     ensure_ascii=False, sort_keys=True, default=str)
    # payload_id 是可见内容版本：采集时间/freshness 心跳不应让墨水屏重刷。
    # publish_id 是传输版本：包含完整元数据，使相同数值的新成功观测仍能上传，
    # 设备据 activity_observed_at 更新“最后成功”而不误判为活动变化。
    versions = meta.get("versions", {}) if isinstance(meta.get("versions"), dict) else {}
    visual_meta = {
        "layout_version": meta.get("layout_version"),
        "font_version": meta.get("font_version"),
        "ai_visual_key": versions.get("ai_visual_key"),
        "news_gold_visual_key": versions.get("news_gold_visual_key"),
        "calendar_key": versions.get("calendar_key"),
        # These switches directly control what the panel draws, so a switch
        # change must produce a new visible payload id as well as a transport id.
        "display_preferences": meta.get("display_preferences"),
    }
    visual_raw = raw + "|visual:" + json.dumps(
        visual_meta, ensure_ascii=False, sort_keys=True, default=str)
    pid = hashlib.md5(visual_raw.encode()).hexdigest()[:16]
    publish_raw = visual_raw + "|transport:" + json.dumps(
        meta, ensure_ascii=False, sort_keys=True, default=str)
    pubid = hashlib.sha256(publish_raw.encode()).hexdigest()[:20]
    screen = {
        "mode_id": "DASHBOARD", "layout_mode": "full", "refresh": "auto",
        "payload_id": pid, "publish_id": pubid, "widgets": [], "modules": modules,
        "footer": {"label": "InkSight"},
    }
    screen.update(meta)
    return {
        "schema_ver": 1, "payload_type": "screen",
        "screen": screen,
    }


def _strip_sensitive(obj):
    """防御性：发布内容中禁止出现凭据/内部认证字段。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            # ``*_tokens`` 是用量计数，不是凭据。仅把单数 token 字段视为
            # 认证信息，避免误删 deepseek_today_tokens 等可公开统计值。
            token_secret = (kl == "token" or kl.endswith("_token") or
                            kl.startswith("token_"))
            if token_secret or any(t in kl for t in (
                    "password", "secret", "api_key", "apikey",
                    "authorization", "cookie", "credential")):
                continue
            out[k] = _strip_sensitive(v)
        return out
    if isinstance(obj, list):
        return [_strip_sensitive(x) for x in obj]
    return obj


def _webdav_request(method, url, headers, body=None, timeout=60):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def publish_one(webdav: str, user: str, password: str, mac: str, payload: dict, dry_run=False):
    """可靠发布（第一阶段）：
      1) 递归剔除敏感字段；
      2) 先 PUT <file>.part → WebDAV MOVE 覆盖（服务端改名，尽可能原子）；
         MOVE 失败回退直接 PUT；
      3) 发布后 GET 回读校验 SHA-256，不匹配则用备份恢复旧文件；
      4) 任何失败返回 False（不更新状态 → 下轮重试；旧文件尽可能保留）。
    """
    import hashlib
    mac = mac.upper().replace(":", "")  # 坚果云拒绝文件名含 ':'：AA:BB:... → AABBCCDDEEFF
    payload = _strip_sensitive(payload)
    url = f"{webdav.rstrip('/')}/{mac}.json"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    if dry_run:
        print(f"[dry] PUT {url} {len(body)}B payload_id={payload_id(payload)} sha={digest[:10]}")
        return True
    headers = {"Content-Type": "application/json"}
    auth = {}
    if user:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        auth = {"Authorization": f"Basic {token}"}
    try:
        # 备份旧文件（若存在）
        backup = None
        try:
            st, old = _webdav_request("GET", url, auth)
            if st == 200:
                backup = old
        except Exception:  # noqa: BLE001  首次发布无旧文件
            backup = None
        # A) part + MOVE（原子替换意图）
        part_url = url + ".part"
        st, _ = _webdav_request("PUT", part_url, {**headers, **auth}, body)
        if st not in (200, 201, 204):
            raise RuntimeError(f"PUT .part HTTP {st}")
        moved = False
        try:
            dest = url
            dest_hdr = dict(auth)
            dest_hdr["Destination"] = dest
            dest_hdr["Overwrite"] = "T"
            st, _ = _webdav_request("MOVE", part_url, dest_hdr)
            moved = st in (200, 201, 204)
        except Exception as e:  # noqa: BLE001
            print(f"[pub ] MOVE 不支持({e})，回退直接 PUT")
        if not moved:
            st, _ = _webdav_request("PUT", url, {**headers, **auth}, body)
            if st not in (200, 201, 204):
                raise RuntimeError(f"PUT final HTTP {st}")
            try:  # 回退路径清掉残留 .part
                _webdav_request("DELETE", part_url, auth)
            except Exception:  # noqa: BLE001
                pass
        # 校验
        st, back = _webdav_request("GET", url, auth)
        if st != 200 or hashlib.sha256(back).hexdigest() != digest:
            # 校验失败：尽力恢复备份
            if backup is not None:
                try:
                    _webdav_request("PUT", url, {**headers, **auth}, backup)
                except Exception:  # noqa: BLE001
                    pass
            raise RuntimeError("远端校验失败（哈希不匹配），已尝试保留旧文件")
        return True
    except Exception as e:  # noqa: BLE001
        hint = _hint_for(getattr(e, "code", None), e)
        print(f"[pub ] 发布失败 {mac}: {hint}")
        return False


def ensure_webdav_dir(webdav: str, user: str, password: str):
    """坚果云的 <MAC>.json 目录不存在时 PUT 会 404——先 MKCOL 建一次（已存在=405，忽略）。"""
    url = webdav.rstrip("/")
    headers = {}
    if user:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    try:
        req = urllib.request.Request(url, data=b"", headers=headers, method="MKCOL")
        with urllib.request.urlopen(req, timeout=20) as r:
            pass  # 201 Created
    except urllib.error.HTTPError as e:
        if e.code not in (405, 409, 301, 302, 200):
            print(f"[warn] MKCOL {url} -> HTTP {e.code}（目录可能已存在，忽略）")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] MKCOL {url} 失败: {e}（稍后 PUT 失败时请手动在坚果云建该目录）")


def _hint_for(code, exc):
    if code == 401:
        return "认证失败：账号或应用密码不对（应用密码 16 位：坚果云→账户信息→安全选项→添加应用密码）"
    if code == 403:
        return "权限不足：该账号不能写这个目录（确认 WebDAV 地址路径正确）"
    if code in (404, 409, 405):
        return "目录不存在且无法自动创建：请先在坚果云网页版手动新建目录（如 inksight）"
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        return "网络不可达：需要能访问 dav.jianguoyun.com（公司网/代理/VPN 需先放行 https）"
    return f"{exc}"


def probe_webdav(webdav: str, user: str, password: str):
    """真连坚果云 WebDAV 做全链路预检：建目录→PUT 探针→GET 回读→DELETE 清理。"""
    base = webdav.rstrip("/")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    hdr = {"Authorization": f"Basic {token}"}
    name = ".inksight_probe"
    url = f"{base}/{name}"
    print(f"[probe] 1/4 确保目录存在: {base}")
    ensure_webdav_dir(webdav, user, password)
    body = json.dumps({"probe": 1, "ts": time.time()}).encode("utf-8")
    print(f"[probe] 2/4 PUT 探针: {url}")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=hdr, method="PUT"), timeout=30) as r:
            if r.status not in (200, 201, 204):
                raise RuntimeError(f"PUT HTTP {r.status}")
    except urllib.error.HTTPError as e:
        print(f"[probe] ❌ FAIL at PUT: {_hint_for(e.code, e)}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[probe] ❌ FAIL at PUT: {_hint_for(None, e)}")
        return False
    print("[probe] 3/4 GET 回读校验")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=30) as r:
            if json.loads(r.read()).get("probe") != 1:
                raise RuntimeError("内容与上传不符")
    except urllib.error.HTTPError as e:
        print(f"[probe] ❌ FAIL at GET: {_hint_for(e.code, e)}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[probe] ❌ FAIL at GET: {_hint_for(None, e)}")
        return False
    print("[probe] 4/4 DELETE 清理探针")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr, method="DELETE"), timeout=30) as r:
            pass
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"[probe] 清理警告 DELETE HTTP {e.code}（可忽略）")
    except Exception as e:  # noqa: BLE001
        print(f"[probe] 清理警告: {e}（可忽略）")
    print("[probe] ✅ PASS：坚果云 WebDAV 建目录/上传/回读/删除 全链路可用")
    return True


def ble_notify(mac: str, broadcast_bin=None):
    """上传成功后才广播：调用 macOS 广播端，设备扫描到即触发拉取。

    优先用 .app 形态（macOS 只有带 Info.plist 的应用才能进入 poweredOn 上空气）；
    没有则退回旧 CLI 二进制（能跑但大概率无法上空气，见 tools/make_mac_ble_app.sh）。
    """
    if not broadcast_bin:
        base = os.path.dirname(os.path.abspath(__file__))
        app_bin = os.path.join(base, "broadcast_mac.app", "Contents", "MacOS", "broadcast_mac")
        broadcast_bin = app_bin if os.path.exists(app_bin) else os.path.join(base, "broadcast_mac")
    if not os.path.exists(broadcast_bin):
        return
    try:
        import subprocess
        subprocess.Popen([broadcast_bin, mac.upper(), "8"])
        print(f"[ble ] 广播触发 {mac}（上传已完成）")
    except Exception as e:  # noqa: BLE001
        print(f"[ble ] 广播失败: {e}")


def _run_unlocked(config, once=False, dry_run=False, force=False):
    state = load_state()
    backend = config["backend"]
    webdav = config["webdav"]
    user = config.get("user", "")
    password = config.get("password", "")
    devices = config["devices"]
    changed_any = False
    admin_token = os.environ.get("ADMIN_TOKEN") or config.get("admin_token", "")
    if not dry_run and user:
        ensure_webdav_dir(webdav, user, password)
    for dev in devices:
        mac = dev["mac"].upper()
        payloads = []
        for mode in dev.get("modes", []):
            try:
                payloads.append(fetch_payload(backend, mode, admin_token=admin_token))
            except Exception as e:  # noqa: BLE001
                print(f"[skip] {mac} {mode} fetch failed: {e}")
        combined = combine_dashboard(payloads)
        if combined is None:
            print(f"[skip] {mac} 无可用内容")
            continue
        pid = payload_id(combined)
        pubid = publication_id(combined)
        if not force and state.get(mac) == pubid:
            print(f"[same] {mac} publication_id 未变，跳过上传")
            continue
        combined["ts"] = int(time.time())  # 上传时刻：设备端活跃/休眠判定的时间源
        ok = publish_one(webdav, user, password, mac, combined, dry_run=dry_run)
        if ok:
            state[mac] = pubid
            changed_any = True
            print(f"[pub ] {mac} -> {pid} 模块 {[m['id'] for m in combined['screen']['modules']]} "
                  f"({len(json.dumps(combined, ensure_ascii=False))}B)")
            if not dry_run and config.get("ble_notify", False):
                ble_notify(mac)  # BLE 模块保留、默认关闭（运行模式=设备 30s 轮询）；将来启用置 true
    save_state(state)
    print(f"完成（{'有更新' if changed_any else '无变化'}）")
    return changed_any


def run(config, once=False, dry_run=False, force=False):
    # Serialize the complete read/publish/state-write cycle, including manual runs.
    lock_fd = os.open(STATE_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        else:  # pragma: no cover - exercised by Windows package
            if os.path.getsize(STATE_LOCK_FILE) == 0:
                os.write(lock_fd, b"\0")
            os.lseek(lock_fd, 0, os.SEEK_SET)
            msvcrt.locking(lock_fd, msvcrt.LK_LOCK, 1)
        return _run_unlocked(config, once=once, dry_run=dry_run, force=force)
    finally:
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        else:  # pragma: no cover
            os.lseek(lock_fd, 0, os.SEEK_SET)
            msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
        os.close(lock_fd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="JSON 配置文件")
    ap.add_argument("--operator-mac", help="从统一私密配置读取 WebDAV 凭据，并发布到该设备 MAC")
    ap.add_argument("--backend", default="http://127.0.0.1:8080")
    ap.add_argument("--webdav")
    ap.add_argument("--user", default="")
    ap.add_argument("--pass", dest="password", default="")
    ap.add_argument("--mac")
    ap.add_argument("--modes", default="AI_USAGE", help="默认 AI_USAGE；热点规则定稿后加 HOTLIST")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--heartbeat", action="store_true", help="无条件上传一次（刷新 ts，供心跳/开机）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--probe", action="store_true", help="坚果云连通预检（无需后端/设备）：建目录→PUT→GET→DELETE")
    args = ap.parse_args()

    if args.probe:
        if args.config:
            c = json.load(open(args.config, encoding="utf-8"))
            webdav, user, password = c["webdav"], c.get("user", ""), c.get("password", "")
        else:
            if not args.webdav:
                sys.exit("--probe 需要 --config 或 --webdav")
            webdav, user, password = args.webdav, args.user, args.password
        sys.exit(0 if probe_webdav(webdav, user, password) else 1)

    if args.operator_mac:
        from runtime_config import from_operator_secrets
        config = from_operator_secrets(args.operator_mac, backend=args.backend)
    elif args.config:
        config = json.load(open(args.config, encoding="utf-8"))
    else:
        if not args.webdav or not args.mac:
            sys.exit("需要 --config 或 --webdav+--mac")
        config = {
            "backend": args.backend, "webdav": args.webdav,
            "user": args.user, "password": args.password,
            "devices": [{"mac": args.mac, "modes": [m.strip() for m in args.modes.split(",")]}],
        }

    if args.once or args.dry_run:
        run(config, once=True, dry_run=args.dry_run, force=args.heartbeat)
        return
    while True:
        run(config)
        time.sleep(300)


if __name__ == "__main__":
    main()
