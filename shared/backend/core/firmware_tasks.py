"""Safe, persistent build and USB-flash jobs for the supported InkSight board.

No browser-supplied command, path, address, chip, or PlatformIO environment is
ever executed.  The web API can select only opaque identifiers issued by this
module after local discovery.
"""
from __future__ import annotations

import copy
import binascii
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import struct
import threading
import time
from pathlib import Path
from typing import Any

from . import operator_config, state_store

TARGET = "epd_426_ssd1677_s3_n32r16"
CHIP = "esp32s3"
APP_OFFSET = "0x10000"
APP1_OFFSET = "0x610000"
APP_SIZE = "0x600000"
NVS_OFFSET = "0x9000"
NVS_SIZE = "0x4000"
OTADATA_OFFSET = "0xd000"
OTADATA_SIZE = "0x2000"
BAUD = "460800"
FLASH_SIZE_BYTES = 32 * 1024 * 1024
PARTITION_OFFSET = "0x8000"
PARTITION_SIZE = "0xc00"
BOOTLOADER_OFFSET = "0x0"
BOOT_APP0_OFFSET = "0xe000"

_ROOT = Path(__file__).resolve().parents[3]
_FIRMWARE = _ROOT / "shared" / "firmware"
_PIO_PYTHON = _ROOT / ".pio-venv" / "bin" / "python"
_ESPTOOL = _ROOT / ".pio-core" / "packages" / "tool-esptoolpy" / "esptool.py"
_BOOT_APP0 = _ROOT / ".pio-core" / "packages" / "framework-arduinoespressif32" / "tools" / "partitions" / "boot_app0.bin"
_STATE = state_store.state_path("firmware_tasks.json")
_PRIVATE = state_store.state_dir() / "firmware-private"
_PORT_RE = re.compile(r"^/dev/(?:cu|tty)\.[A-Za-z0-9._-]+$")
_MAC_RE = re.compile(r"(?im)^MAC:\s*([0-9a-f]{2}(?::[0-9a-f]{2}){5})\s*$")
_BUILD_LOCK = threading.Lock()
_FLASH_LOCK = threading.Lock()
_THREADS: dict[str, threading.Thread] = {}
_THREADS_LOCK = threading.Lock()
_DEVICE_CACHE: dict[str, dict[str, Any]] = {}
_CONFIRM_SECRET = os.urandom(32)


def _device_server_url() -> str:
    """Return the LAN URL an attached device can use for upgrade confirmation."""
    explicit = str(os.environ.get("INKSIGHT_DEVICE_SERVER_URL") or "").strip().rstrip("/")
    if explicit:
        if not re.match(r"^https?://[A-Za-z0-9._:\[\]-]+$", explicit):
            raise RuntimeError("INKSIGHT_DEVICE_SERVER_URL 格式无效")
        return explicit
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect selects the active route without sending application data.
        sock.connect(("192.0.2.1", 9))
        address = str(sock.getsockname()[0] or "")
    except OSError:
        return ""
    finally:
        sock.close()
    if not address or address.startswith("127."):
        return ""
    port = int(os.environ.get("INKSIGHT_DEVICE_SERVER_PORT") or "8080")
    if not 1 <= port <= 65535:
        raise RuntimeError("INKSIGHT_DEVICE_SERVER_PORT 格式无效")
    return f"http://{address}:{port}"


def configure_paths(*, state_file: Path | None = None, private_dir: Path | None = None,
                    root: Path | None = None) -> None:
    global _STATE, _PRIVATE, _ROOT, _FIRMWARE, _PIO_PYTHON, _ESPTOOL, _BOOT_APP0
    if state_file is not None:
        _STATE = Path(state_file)
    if private_dir is not None:
        _PRIVATE = Path(private_dir)
    if root is not None:
        _ROOT = Path(root)
        _FIRMWARE = _ROOT / "shared" / "firmware"
        _PIO_PYTHON = _ROOT / ".pio-venv" / "bin" / "python"
        _ESPTOOL = _ROOT / ".pio-core" / "packages" / "tool-esptoolpy" / "esptool.py"
        _BOOT_APP0 = _ROOT / ".pio-core" / "packages" / "framework-arduinoespressif32" / "tools" / "partitions" / "boot_app0.bin"


def _empty() -> dict:
    return {"builds": {}, "plans": {}, "flashes": {}}


def _read() -> dict:
    value, error = state_store.read_json(_STATE)
    if error == "missing":
        return _empty()
    if error or not isinstance(value, dict):
        raise state_store.StateStoreError(f"{_STATE.name}: {error or 'invalid'}")
    for name in ("builds", "plans", "flashes"):
        value.setdefault(name, {})
    return value


def _update(updater):
    return state_store.update_json(_STATE, updater, default=_empty())


def _input_snapshot() -> tuple[dict, dict, str]:
    effective = operator_config.load_effective()
    public = copy.deepcopy(effective.config)
    private = copy.deepcopy(effective._secrets)
    raw = repr((public, private, TARGET)).encode("utf-8")
    return public, private, hashlib.sha256(raw).hexdigest()


def _source_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted(_FIRMWARE.rglob("*")):
        if not path.is_file() or any(part in {".pio", ".git", "__pycache__"} for part in path.parts):
            continue
        digest.update(str(path.relative_to(_FIRMWARE)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


@contextlib.contextmanager
def _exclusive_lock(name: str):
    _PRIVATE.mkdir(parents=True, exist_ok=True)
    os.chmod(_PRIVATE, 0o700)
    path = _PRIVATE / f".{name}.lock"
    with path.open("a+b") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _public_job(job: dict) -> dict:
    hidden = {"port", "mac", "confirmation_token", "input_hash", "input_fingerprint", "private_dir"}
    result = {key: copy.deepcopy(value) for key, value in job.items() if key not in hidden}
    for key in ("log", "error"):
        if isinstance(result.get(key), str):
            for private_value in (job.get("port"), job.get("mac")):
                if private_value:
                    result[key] = result[key].replace(str(private_value), "<hidden>")
    return result


def _safe_environment(private: dict) -> tuple[dict[str, str], list[str]]:
    networks = list((private.get("wifi") or {}).get("networks") or [])
    primary = networks[0] if networks else {"ssid": "", "password": ""}
    extra = "^".join(f"{row['ssid']}~{row['password']}" for row in networks[1:])
    cloud = private.get("cloud") or {}
    env = os.environ.copy()
    injected = {
        "INK_SSID": str(primary.get("ssid") or ""),
        "INK_PASS": str(primary.get("password") or ""),
        "INK_EXTRA_WIFI": extra,
        "INK_SERVER": _device_server_url(),
        "CLOUD_BASE_URL": str(cloud.get("base_url") or ""),
        "CLOUD_USER": str(cloud.get("user") or ""),
        "CLOUD_PASS": str(cloud.get("password") or ""),
        "INKSIGHT_BUILD_ID": "",
        # Keep the build on the repository-pinned PlatformIO packages.  If this
        # is omitted PlatformIO may silently fall back to ~/.platformio, which
        # can supply a different boot_app0.bin than the one preflight verified.
        "PLATFORMIO_CORE_DIR": str(_ROOT / ".pio-core"),
    }
    env.update(injected)
    redactions = [value for value in injected.values() if value]
    return env, redactions


def _redact(value: str, secrets_to_hide: list[str]) -> str:
    text = value
    for secret in sorted(secrets_to_hide, key=len, reverse=True):
        text = text.replace(secret, "<hidden>")
    return text[-12000:]


def _flash_metadata(output: str) -> dict[str, tuple[str, Path]]:
    rows: dict[str, tuple[str, Path]] = {}
    for raw in output.splitlines():
        match = re.match(r"^\s*(0x[0-9A-Fa-f]+)\s*->\s*(.+?)\s*$", raw)
        app = re.match(r"^\s*APP\s*->\s*(0x[0-9A-Fa-f]+)\s+(.+?)\s*$", raw)
        if app:
            rows["firmware.bin"] = (hex(int(app.group(1), 16)), Path(app.group(2)))
        elif match:
            rows[Path(match.group(2)).name] = (hex(int(match.group(1), 16)), Path(match.group(2)))
    expected = {
        "bootloader.bin": BOOTLOADER_OFFSET,
        "partitions.bin": PARTITION_OFFSET,
        "boot_app0.bin": BOOT_APP0_OFFSET,
        "firmware.bin": APP_OFFSET,
    }
    if set(rows) != set(expected) or any(rows[name][0] != hex(int(offset, 16)) for name, offset in expected.items()):
        raise RuntimeError("构建工具返回的镜像清单与固定硬件目标不一致")
    return rows


def _run_command(args: list[str], *, timeout: int, env: dict[str, str] | None = None,
                 cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}"


def preflight() -> dict:
    public, private, input_hash = _input_snapshot()
    missing = [str(path) for path in (_PIO_PYTHON, _ESPTOOL, _BOOT_APP0, _FIRMWARE / "platformio.ini") if not path.is_file()]
    networks = list((private.get("wifi") or {}).get("networks") or [])
    return {
        "ready": not missing and bool(networks),
        "target": TARGET,
        "chip": "ESP32-S3",
        "flash_size": "32MB",
        "screen": "4.26 英寸 800 x 480 SSD1677",
        "wifi_count": len(networks),
        "cloud_configured": bool((private.get("cloud") or {}).get("base_url")),
        "config_fingerprint": operator_config._public_fingerprint(public),
        "missing": [Path(item).name for item in missing],
        "writes_device": False,
    }


def create_build() -> dict:
    public, private, input_hash = _input_snapshot()
    check = preflight()
    if not check["ready"]:
        raise RuntimeError("firmware preflight is not ready")
    task_id = _new_id("build")
    now = int(time.time())
    job = {
        "id": task_id,
        "status": "queued",
        "target": TARGET,
        "chip": "ESP32-S3",
        "created_at": now,
        "updated_at": now,
        "config_fingerprint": operator_config._public_fingerprint(public),
        "input_hash": input_hash,
        "source_hash": _source_hash(),
        "wifi_count": len((private.get("wifi") or {}).get("networks") or []),
        "cloud_configured": bool((private.get("cloud") or {}).get("base_url")),
        "artifacts": {},
        "log": "",
    }
    _update(lambda value: value.setdefault("builds", {}).update({task_id: job}))
    return _public_job(job)


def _set(group: str, task_id: str, **fields) -> dict:
    def update(value: dict) -> dict:
        job = value.setdefault(group, {}).get(task_id)
        if not isinstance(job, dict):
            raise KeyError(task_id)
        job.update(copy.deepcopy(fields))
        job["updated_at"] = int(time.time())
        return _public_job(job)
    return _update(update)


def run_build(task_id: str) -> dict:
    job = (_read().get("builds") or {}).get(task_id)
    if not isinstance(job, dict):
        raise KeyError(task_id)
    if job.get("status") == "completed":
        return _public_job(job)
    with _BUILD_LOCK, _exclusive_lock("build"):
        _set("builds", task_id, status="running", started_at=int(time.time()))
        _public, private, current_hash = _input_snapshot()
        if current_hash != job.get("input_hash"):
            return _set("builds", task_id, status="stale", error="配置已变化，请重新创建构建")
        if _source_hash() != job.get("source_hash"):
            return _set("builds", task_id, status="stale", error="固件源码已变化，请重新创建构建")
        private_dir = _PRIVATE / task_id
        private_dir.mkdir(parents=True, exist_ok=False)
        os.chmod(private_dir, 0o700)
        env, redactions = _safe_environment(private)
        env["INKSIGHT_BUILD_ID"] = task_id
        env["PLATFORMIO_BUILD_DIR"] = str(private_dir / "build")
        args = [str(_PIO_PYTHON), "-m", "platformio", "run", "-d", str(_FIRMWARE), "-e", TARGET]
        try:
            result = _run_command(args, timeout=20 * 60, env=env, cwd=_ROOT)
        except subprocess.TimeoutExpired:
            return _set("builds", task_id, status="failed", error="构建超过 20 分钟")
        log = _redact((result.stdout or "") + "\n" + (result.stderr or ""), redactions)
        if result.returncode != 0:
            return _set("builds", task_id, status="failed", error="构建失败", log=log)
        source = private_dir / "build" / TARGET
        required = ["firmware.bin", "bootloader.bin", "partitions.bin"]
        if any(not (source / name).is_file() for name in required):
            return _set("builds", task_id, status="failed", error="构建产物不完整", log=log)
        try:
            metadata = _flash_metadata((result.stdout or "") + "\n" + (result.stderr or ""))
        except RuntimeError as exc:
            return _set("builds", task_id, status="failed", error=str(exc), log=log)
        sources = {name: metadata[name][1] for name in metadata}
        if any(sources[name].resolve() != (source / name).resolve() for name in required):
            return _set("builds", task_id, status="failed", error="构建镜像路径不属于本次隔离目录", log=log)
        if sources["boot_app0.bin"].resolve() != _BOOT_APP0.resolve():
            return _set("builds", task_id, status="failed", error="OTA 初始化镜像来源异常", log=log)
        manifest = {
            "bootloader.bin": (BOOTLOADER_OFFSET, "bootloader"),
            "partitions.bin": (PARTITION_OFFSET, "partition-table"),
            "boot_app0.bin": (BOOT_APP0_OFFSET, "ota-initializer"),
            "firmware.bin": (APP_OFFSET, "application"),
        }
        artifacts = {}
        for name, original in sources.items():
            destination = private_dir / name
            shutil.copy2(original, destination)
            os.chmod(destination, 0o600)
            artifacts[name] = {
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "bytes": destination.stat().st_size,
                "offset": manifest[name][0],
                "role": manifest[name][1],
            }
        if artifacts["firmware.bin"]["bytes"] > int(APP_SIZE, 16):
            return _set("builds", task_id, status="failed", error="应用镜像超过分区容量", log=log)
        if _source_hash() != job.get("source_hash"):
            return _set("builds", task_id, status="stale", error="构建期间固件源码发生变化", log=log)
        return _set(
            "builds", task_id, status="completed", finished_at=int(time.time()),
            artifacts=artifacts, private_dir=str(private_dir), log=log,
        )


def start_build(task_id: str) -> dict:
    with _THREADS_LOCK:
        existing = _THREADS.get(task_id)
        if not (existing and existing.is_alive()):
            worker = threading.Thread(target=run_build, args=(task_id,), daemon=True, name=f"inksight-{task_id}")
            _THREADS[task_id] = worker
            worker.start()
    return get_build(task_id)


def get_build(task_id: str) -> dict:
    job = (_read().get("builds") or {}).get(task_id)
    if not isinstance(job, dict):
        raise KeyError(task_id)
    result = _public_job(job)
    try:
        _public, _private, current_hash = _input_snapshot()
        result["stale"] = current_hash != job.get("input_hash") or _source_hash() != job.get("source_hash")
    except Exception:
        result["stale"] = True
    return result


def _list_ports():
    try:
        from serial.tools import list_ports
        return list(list_ports.comports())
    except ImportError:
        script = (
            "import json; from serial.tools import list_ports; "
            "print(json.dumps([{'device':p.device,'description':p.description,'vid':p.vid,'pid':p.pid} "
            "for p in list_ports.comports()]))"
        )
        result = _run_command([str(_PIO_PYTHON), "-c", script], timeout=15, cwd=_ROOT)
        if result.returncode != 0:
            return []
        try:
            rows = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return []
        return [type("SerialPort", (), row)() for row in rows if isinstance(row, dict)]


def detect_devices() -> list[dict]:
    candidates = []
    cache: dict[str, dict[str, Any]] = {}
    for port in _list_ports():
        path = str(getattr(port, "device", "") or "")
        description = str(getattr(port, "description", "") or "")
        if not _PORT_RE.match(path):
            continue
        haystack = (path + " " + description).lower()
        if not any(word in haystack for word in ("usb", "uart", "serial", "cp210", "ch34", "wch", "modem")):
            continue
        identity = f"{path}|{getattr(port, 'vid', '')}|{getattr(port, 'pid', '')}"
        device_id = "usb-" + hmac.new(_CONFIRM_SECRET, identity.encode(), hashlib.sha256).hexdigest()[:16]
        cache[device_id] = {"port": path, "description": description}
        candidates.append({"id": device_id, "label": description or "USB 串口", "path_hint": Path(path).name})
    _DEVICE_CACHE.clear()
    _DEVICE_CACHE.update(cache)
    return candidates


def _esptool(port: str, *args: str) -> list[str]:
    return [str(_PIO_PYTHON), str(_ESPTOOL), "--chip", CHIP, "--port", port, *args]


def _port_owner(port: str) -> str | None:
    result = _run_command(["/usr/sbin/lsof", "-Fn", "--", port], timeout=10, cwd=_ROOT)
    if result.returncode not in (0, 1):
        raise RuntimeError("无法确认串口占用状态")
    names = [line[1:] for line in (result.stdout or "").splitlines() if line.startswith("n")]
    return names[0] if names else None


def _identify(port: str) -> tuple[str, str]:
    if _port_owner(port):
        raise RuntimeError("串口正被其他程序占用，请先正常释放后重试")
    mac_result = _run_command(_esptool(port, "read_mac"), timeout=30, cwd=_ROOT)
    output = (mac_result.stdout or "") + "\n" + (mac_result.stderr or "")
    match = _MAC_RE.search(output)
    if mac_result.returncode != 0 or not match or "ESP32-S3" not in output.upper():
        raise RuntimeError("未能确认目标为 ESP32-S3")
    flash_result = _run_command(_esptool(port, "flash_id"), timeout=30, cwd=_ROOT)
    flash_output = (flash_result.stdout or "") + "\n" + (flash_result.stderr or "")
    if flash_result.returncode != 0 or not re.search(r"(?:Detected flash size|Flash size):\s*32MB", flash_output, re.I):
        raise RuntimeError("目标 Flash 不是已确认的 32MB 容量")
    return match.group(1).upper(), "32MB"


def _read_region(port: str, offset: str, size: str, path: Path, *, timeout: int = 180) -> bytes:
    try:
        result = _run_command(
            _esptool(port, "--baud", BAUD, "read_flash", offset, size, str(path)),
            timeout=timeout, cwd=_ROOT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"读取设备区域 {offset} 超时，未执行写入") from exc
    if result.returncode != 0 or not path.is_file() or path.stat().st_size != int(size, 16):
        raise RuntimeError(f"读取设备区域 {offset} 失败")
    os.chmod(path, 0o600)
    return path.read_bytes()


def _artifact_path(build: dict, name: str) -> Path:
    path = Path(str(build.get("private_dir") or "")) / name
    expected = ((build.get("artifacts") or {}).get(name) or {}).get("sha256")
    if not path.is_file() or not expected or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise RuntimeError(f"构建产物 {name} 缺失或已被修改")
    return path


def _partition_state(raw: bytes, expected: bytes) -> str:
    if raw and all(value == 0xFF for value in raw):
        return "blank"
    if hmac.compare_digest(raw, expected):
        return "matching"
    return "mismatch"


def _require_fresh_blank(port: str, directory: Path, prefix: str) -> None:
    probes = (
        (BOOTLOADER_OFFSET, "0x8000", "boot"),
        (NVS_OFFSET, NVS_SIZE, "nvs"),
        (OTADATA_OFFSET, OTADATA_SIZE, "otadata"),
        (APP_OFFSET, "0x1000", "app0"),
    )
    for offset, size, name in probes:
        raw = _read_region(port, offset, size, directory / f"{prefix}-{name}.bin")
        if not raw or not all(value == 0xFF for value in raw):
            raise RuntimeError("空白板安装检测到已有数据，已拒绝初始化")


def prepare_flash(build_id: str, device_id: str, install_mode: str = "update") -> dict:
    if install_mode not in {"update", "fresh"}:
        raise RuntimeError("安装模式无效")
    build = (_read().get("builds") or {}).get(build_id)
    device = _DEVICE_CACHE.get(device_id)
    if not isinstance(build, dict) or build.get("status") != "completed":
        raise RuntimeError("build is not ready")
    if not device:
        raise RuntimeError("device list changed; detect again")
    if get_build(build_id).get("stale"):
        raise RuntimeError("build is stale")
    plan_id = _new_id("flash-plan")
    private_dir = _PRIVATE / plan_id
    private_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(private_dir, 0o700)
    mac, flash_size = _identify(str(device["port"]))
    partition_raw = _read_region(str(device["port"]), PARTITION_OFFSET, PARTITION_SIZE, private_dir / "partition.bin")
    expected_partition = _artifact_path(build, "partitions.bin").read_bytes()
    if len(expected_partition) != int(PARTITION_SIZE, 16):
        raise RuntimeError("构建分区表长度与目标不一致")
    partition_state = _partition_state(partition_raw, expected_partition)
    if install_mode == "update" and partition_state != "matching":
        raise RuntimeError("已有设备更新要求分区表完全匹配")
    if install_mode == "fresh" and partition_state != "blank":
        raise RuntimeError("空白板安装只允许分区表区域完全空白的设备")
    if install_mode == "fresh":
        _require_fresh_blank(str(device["port"]), private_dir, "prepare")
    for name in ("bootloader.bin", "partitions.bin", "boot_app0.bin", "firmware.bin"):
        _artifact_path(build, name)
    token = os.urandom(24).hex()
    now = int(time.time())
    plan = {
        "id": plan_id, "build_id": build_id, "device_id": device_id,
        "status": "ready", "target": TARGET, "chip": "ESP32-S3",
        "device_fingerprint": hashlib.sha256(mac.encode()).hexdigest()[:12],
        "artifact_sha256": build["artifacts"]["firmware.bin"]["sha256"],
        "artifact_manifest": copy.deepcopy(build["artifacts"]),
        "source_hash": build.get("source_hash"), "install_mode": install_mode,
        "flash_size": flash_size, "partition_state": partition_state,
        "port": device["port"], "mac": mac, "confirmation_token": token,
        "created_at": now, "expires_at": now + 5 * 60,
        "scope": (
            "写入未运行的 OTA 应用分区，校验后再切换启动项；NVS 保留并备份"
            if install_mode == "update" else
            "仅写入引导程序、分区表、OTA 初始化镜像和 app0；不执行全盘擦除"
        ),
    }
    _update(lambda value: value.setdefault("plans", {}).update({plan_id: plan}))
    public = _public_job(plan)
    public["confirmation_token"] = token
    return public


def create_flash(plan_id: str, confirmation_token: str) -> dict:
    flash_id = _new_id("flash")
    now = int(time.time())
    initial_plan = (_read().get("plans") or {}).get(plan_id)
    if not isinstance(initial_plan, dict):
        raise RuntimeError("flash plan is not ready")
    if get_build(str(initial_plan.get("build_id") or "")).get("stale"):
        raise RuntimeError("build is stale")
    holder = {}
    def consume(value: dict) -> dict:
        plan = value.setdefault("plans", {}).get(plan_id)
        if not isinstance(plan, dict) or plan.get("status") != "ready":
            raise RuntimeError("flash plan is not ready or was already used")
        if int(plan.get("expires_at") or 0) < now:
            raise RuntimeError("flash confirmation expired")
        if not hmac.compare_digest(str(plan.get("confirmation_token") or ""), confirmation_token or ""):
            raise RuntimeError("flash confirmation token is invalid")
        plan["status"] = "consumed"
        job = {
            "id": flash_id, "plan_id": plan_id, "build_id": plan["build_id"],
            "install_mode": plan["install_mode"], "partition_state": plan["partition_state"],
            "source_hash": plan.get("source_hash"), "status": "queued", "port": plan["port"],
            "mac": plan["mac"], "device_fingerprint": plan["device_fingerprint"],
            "target": TARGET, "created_at": now, "updated_at": now, "verification": "pending",
        }
        value.setdefault("flashes", {})[flash_id] = job
        holder.update(job)
        return value
    _update(consume)
    job = holder
    return _public_job(job)


def _valid_ota_records(raw: bytes) -> list[tuple[int, int]]:
    rows = []
    for index, start in enumerate((0, 0x1000)):
        if len(raw) < start + 32:
            continue
        seq = struct.unpack_from("<I", raw, start)[0]
        crc = struct.unpack_from("<I", raw, start + 28)[0]
        expected = binascii.crc32(struct.pack("<I", seq), 0xFFFFFFFF) & 0xFFFFFFFF
        if seq not in (0, 0xFFFFFFFF) and crc == expected:
            rows.append((seq, index))
    return rows


def _next_ota_data(raw: bytes) -> tuple[bytes, int]:
    valid = _valid_ota_records(raw)
    current_slot = (max(valid)[0] - 1) % 2 if valid else 0
    target_slot = 1 - current_slot
    seq = (max((row[0] for row in valid), default=0) + 1) & 0xFFFFFFFF
    while seq in (0, 0xFFFFFFFF) or (seq - 1) % 2 != target_slot:
        seq = (seq + 1) & 0xFFFFFFFF
    destination_record = 1 - max(valid, default=(0, 1))[1]
    output = bytearray(raw if len(raw) == int(OTADATA_SIZE, 16) else b"\xff" * int(OTADATA_SIZE, 16))
    start = destination_record * 0x1000
    output[start:start + 32] = b"\xff" * 32
    struct.pack_into("<I", output, start, seq)
    struct.pack_into("<I", output, start + 28, binascii.crc32(struct.pack("<I", seq), 0xFFFFFFFF) & 0xFFFFFFFF)
    return bytes(output), target_slot


def _step(args: list[str], logs: list[str], label: str, timeout: int = 180) -> None:
    try:
        result = _run_command(args, timeout=timeout, cwd=_ROOT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{label}超时") from exc
    logs.append((result.stdout or "") + "\n" + (result.stderr or ""))
    if result.returncode != 0:
        raise RuntimeError(f"{label}失败")


def run_flash(flash_id: str) -> dict:
    state = _read()
    job = (state.get("flashes") or {}).get(flash_id)
    if not isinstance(job, dict):
        raise KeyError(flash_id)
    build = (state.get("builds") or {}).get(job.get("build_id"))
    if not isinstance(build, dict) or build.get("status") != "completed":
        return _set("flashes", flash_id, status="failed", error="构建产物不可用")
    with _FLASH_LOCK, _exclusive_lock("flash"):
        _set("flashes", flash_id, status="running", started_at=int(time.time()))
        backup = _PRIVATE / flash_id / "backup"
        backup.mkdir(parents=True, exist_ok=True)
        os.chmod(backup.parent, 0o700)
        os.chmod(backup, 0o700)
        logs = []
        try:
            build_public = get_build(str(job["build_id"]))
            if build_public.get("stale") or build.get("source_hash") != job.get("source_hash"):
                raise RuntimeError("构建快照已变化")
            for name in ("bootloader.bin", "partitions.bin", "boot_app0.bin", "firmware.bin"):
                _artifact_path(build, name)
            mac, _flash_size = _identify(str(job["port"]))
            if not hmac.compare_digest(mac, str(job["mac"])):
                raise RuntimeError("执行前检测到设备已更换")
            partition_raw = _read_region(str(job["port"]), PARTITION_OFFSET, PARTITION_SIZE, backup / "partition.bin")
            partition_expected = _artifact_path(build, "partitions.bin").read_bytes()
            state = _partition_state(partition_raw, partition_expected)
            if state != job.get("partition_state"):
                raise RuntimeError("执行前分区状态发生变化")
            base = _esptool(str(job["port"]))
            if job.get("install_mode") == "update":
                nvs = _read_region(str(job["port"]), NVS_OFFSET, NVS_SIZE, backup / "nvs.bin")
                ota = _read_region(str(job["port"]), OTADATA_OFFSET, OTADATA_SIZE, backup / "otadata.bin")
                next_ota, target_slot = _next_ota_data(ota)
                app_offset = APP1_OFFSET if target_slot == 1 else APP_OFFSET
                _read_region(str(job["port"]), app_offset, APP_SIZE, backup / f"app{target_slot}.bin", timeout=8 * 60)
                firmware = _artifact_path(build, "firmware.bin")
                _step(base + ["--baud", BAUD, "write_flash", app_offset, str(firmware)], logs, "写入应用", 8 * 60)
                _step(base + ["verify_flash", app_offset, str(firmware)], logs, "校验应用", 8 * 60)
                ota_path = backup.parent / "otadata-next.bin"
                ota_path.write_bytes(next_ota)
                os.chmod(ota_path, 0o600)
                _step(base + ["--baud", BAUD, "write_flash", OTADATA_OFFSET, str(ota_path)], logs, "切换启动分区")
                _step(base + ["verify_flash", OTADATA_OFFSET, str(ota_path)], logs, "校验启动分区")
                _ = nvs
            else:
                _require_fresh_blank(str(job["port"]), backup, "execute")
                for name in ("bootloader.bin", "partitions.bin", "boot_app0.bin", "firmware.bin"):
                    artifact = _artifact_path(build, name)
                    offset = str(build["artifacts"][name]["offset"])
                    _step(base + ["--baud", BAUD, "write_flash", offset, str(artifact)], logs, f"写入{name}", 8 * 60)
                    _step(base + ["verify_flash", offset, str(artifact)], logs, f"校验{name}", 8 * 60)
            verified_at = int(time.time())
            _set(
                "flashes", flash_id, status="waiting_heartbeat", verified_at=verified_at,
                heartbeat_deadline=verified_at + 10 * 60,
                verification="flash-verified; rebooting",
                backup_available=True, log="\n".join(logs)[-12000:],
            )
            _step(base + ["run"], logs, "重启设备")
            return _set(
                "flashes", flash_id, status="waiting_heartbeat", verified_at=verified_at,
                heartbeat_deadline=verified_at + 10 * 60, verification="flash-verified; rebooted",
                backup_available=True, log="\n".join(logs)[-12000:],
            )
        except RuntimeError as exc:
            return _set(
                "flashes", flash_id, status="failed", error=str(exc),
                backup_available=any(backup.iterdir()), log="\n".join(logs)[-12000:],
            )


def start_flash(flash_id: str) -> dict:
    with _THREADS_LOCK:
        existing = _THREADS.get(flash_id)
        if not (existing and existing.is_alive()):
            worker = threading.Thread(target=run_flash, args=(flash_id,), daemon=True, name=f"inksight-{flash_id}")
            _THREADS[flash_id] = worker
            worker.start()
    job = (_read().get("flashes") or {}).get(flash_id)
    if not isinstance(job, dict):
        raise KeyError(flash_id)
    return _public_job(job)


def get_flash(flash_id: str) -> dict:
    job = (_read().get("flashes") or {}).get(flash_id)
    if not isinstance(job, dict):
        raise KeyError(flash_id)
    if job.get("status") == "running":
        with _THREADS_LOCK:
            worker = _THREADS.get(flash_id)
            alive = bool(worker and worker.is_alive())
        if not alive:
            return _set(
                "flashes", flash_id, status="failed",
                error="任务异常结束，设备未完成写入；请重新识别后重试",
                backup_available=bool((_PRIVATE / flash_id / "backup").exists()),
            )
    if job.get("status") == "waiting_heartbeat" and int(job.get("heartbeat_deadline") or 0) < int(time.time()):
        return _set("flashes", flash_id, status="network_timeout", error="写入已校验，但未收到匹配的设备联网心跳")
    return _public_job(job)


def get_flash_identity(flash_id: str) -> dict:
    job = (_read().get("flashes") or {}).get(flash_id)
    if not isinstance(job, dict):
        raise KeyError(flash_id)
    return {"mac": str(job.get("mac") or ""), "build_id": str(job.get("build_id") or "")}


def accepts_upgrade_heartbeat(mac: str, build_id: str) -> bool:
    """Allow one exact post-flash identity to bootstrap normal device auth."""
    if not mac or not build_id:
        return False
    now = int(time.time())
    for job in (_read().get("flashes") or {}).values():
        if not isinstance(job, dict) or job.get("status") != "waiting_heartbeat":
            continue
        if int(job.get("heartbeat_deadline") or 0) < now:
            continue
        if hmac.compare_digest(str(job.get("mac") or "").upper(), mac.upper()) and hmac.compare_digest(
            str(job.get("build_id") or ""), build_id
        ):
            return True
    return False


def confirm_heartbeat(flash_id: str, heartbeat: dict | None) -> dict:
    job = (_read().get("flashes") or {}).get(flash_id)
    if not isinstance(job, dict):
        raise KeyError(flash_id)
    # A verified image remains valid when the network confirmation arrives
    # after the ten-minute UI deadline.  The heartbeat stored here has already
    # passed normal device authentication; allow it to close a timeout record
    # instead of leaving a successfully upgraded device permanently ambiguous.
    if job.get("status") not in {"waiting_heartbeat", "network_timeout"} or not isinstance(heartbeat, dict):
        return get_flash(flash_id)
    created = str(heartbeat.get("created_at") or "")
    try:
        from datetime import datetime
        heartbeat_epoch = int(datetime.fromisoformat(created).timestamp())
    except (TypeError, ValueError):
        return get_flash(flash_id)
    if heartbeat_epoch <= int(job.get("verified_at") or 0):
        return get_flash(flash_id)
    if not hmac.compare_digest(str(heartbeat.get("firmware_build_id") or ""), str(job.get("build_id") or "")):
        return get_flash(flash_id)
    return _set(
        "flashes", flash_id, status="completed", finished_at=int(time.time()),
        verification="flash-verified; matching-network-heartbeat",
        error=None,
    )
