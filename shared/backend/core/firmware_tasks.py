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
import tempfile
import time
from pathlib import Path
from typing import Any

from . import operator_config, state_store
from .desktop_service import critical_entry, gate

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
_PROBE_CACHE: dict[str, dict[str, Any]] = {}
_CONFIRM_SECRET = os.urandom(32)
_BUILD_CANCEL: dict[str, threading.Event] = {}


class BuildCancelled(RuntimeError):
    pass


def _build_confirmation(input_hash: str, source_hash: str, state: dict) -> str:
    # A confirmation is invalid after either inputs OR the job history changes.
    body = json.dumps([input_hash, source_hash, sorted(state.get('builds', {}))]).encode()
    return hmac.new(_CONFIRM_SECRET, body, hashlib.sha256).hexdigest()


def status_snapshot() -> dict:
    from . import firmware_setup
    preparation = firmware_setup.status()
    flight = preflight()
    state = _read()
    _, _, current_input = _input_snapshot()
    current_source = _source_hash()
    history = {}
    for group in ['builds', 'plans', 'flashes', 'rollbacks']:
        rows = sorted(state.get(group, {}).values(), key=lambda row: (row.get('created_at', 0), row['id']), reverse=True)[:8]
        history[group] = [_public_job(row) for row in rows]
        if group == 'builds':
            for raw, public in zip(rows, history[group]):
                public['stale'] = raw.get('input_hash') != current_input or raw.get('source_hash') != current_source
    flight['build_confirmation'] = _build_confirmation(current_input, current_source, state)
    return {'preparation': preparation, 'preflight': flight, 'history': history}


def _device_server_url() -> str:
    """Return the LAN URL an attached device can use for upgrade confirmation."""
    if os.environ.get('INKSIGHT_DESKTOP_MODE')=='1':
        # Desktop remains loopback-only. Do not advertise an unreachable LAN
        # port or silently open a listener. USB receipts confirm cloud mode.
        return ''
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
    return {"builds": {}, "plans": {}, "flashes": {}, "rollback_plans": {}, "rollbacks": {}}


def _read() -> dict:
    value, error = state_store.read_json(_STATE)
    if error == "missing":
        return _empty()
    if error or not isinstance(value, dict):
        raise state_store.StateStoreError(f"{_STATE.name}: {error or 'invalid'}")
    for name in ("builds", "plans", "flashes", "rollback_plans", "rollbacks"):
        value.setdefault(name, {})
    return value


def _update(updater):
    return state_store.update_json(_STATE, updater, default=_empty())


def _input_snapshot() -> tuple[dict, dict, str]:
    effective = operator_config.load_effective(use_last_known_good=False)
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
    from . import firmware_setup
    for value in (firmware_setup.fonts_status(),firmware_setup.tools_status()):
        digest.update(str(value.get('sha256') or 'missing').encode())
    for name in ['firmware_pio.py','import_misans.py','misans_raster.py','font_gen_ttf.py']:
        digest.update((_ROOT/'shared/tools'/name).read_bytes())
    for name in ['requirements-firmware-py311-macos-arm64.lock','requirements-esptool-py311-macos-arm64.lock']:
        digest.update((_ROOT/name).read_bytes())
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
    hidden = {"port", "mac", "confirmation_token", "input_hash", "input_fingerprint", "private_dir", "build_confirmation"}
    result = {key: copy.deepcopy(value) for key, value in job.items() if key not in hidden}
    for key in ("log", "error"):
        if isinstance(result.get(key), str):
            result[key]=re.sub(r'(?i)\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b','<device-hidden>',result[key])
            for private_value in (job.get("port"), job.get("mac")):
                if private_value:
                    result[key] = result[key].replace(str(private_value), "<hidden>")
    return result


def _safe_environment(private: dict) -> tuple[dict[str, str], list[str]]:
    networks = list((private.get("wifi") or {}).get("networks") or [])
    primary = networks[0] if networks else {"ssid": "", "password": ""}
    extra = "^".join(f"{row['ssid']}~{row['password']}" for row in networks[1:])
    cloud = private.get("cloud") or {}
    from . import firmware_setup
    paths=firmware_setup.tool_paths()
    env = firmware_setup._env(paths['generation']) if paths else os.environ.copy()
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
        "PLATFORMIO_CORE_DIR": str(paths['core'] if paths else _ROOT / ".pio-core"),
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


def _run_build_command(args: list[str], *, timeout: int, env: dict[str, str],
                       cwd: Path) -> subprocess.CompletedProcess:
    """Timeout owns the entire compiler group, never a USB/service process."""
    import signal
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(args, cwd=str(cwd), env=env, stdout=output,
                                   stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            deadline = time.monotonic() + timeout
            cancellation = _BUILD_CANCEL.get(env.get('INKSIGHT_BUILD_ID', ''))
            while process.poll() is None:
                if cancellation and cancellation.is_set():
                    raise BuildCancelled('构建已取消，原有配置与可用固件保留')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                try: process.wait(timeout=min(remaining, .2))
                except subprocess.TimeoutExpired: pass
        except BaseException:
            try:os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:pass
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait()
            raise
        output.seek(0, os.SEEK_END)
        length=output.tell()
        # Keep build diagnostics bounded; secrets are redacted by the caller.
        output.seek(max(0, length - 2 * 1024**2))
        return subprocess.CompletedProcess(args, process.returncode, output.read().decode('utf-8', errors='replace'), '')


def _new_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}"


def preflight() -> dict:
    from . import firmware_setup
    checks={name:getattr(firmware_setup,name+'_status')() for name in ['host','fonts','tools','storage']}
    try:
        public, private, input_hash = _input_snapshot()
        checks['configuration']={'ready':True,'state':'ready','detail':'只使用已应用的生效配置，未应用草稿不会进入构建'}
    except operator_config.ConfigError:
        public,private={},{}
        checks['configuration']={'ready':False,'state':'missing','detail':'请在设备与运行、API与账户中保存并应用配置'}
    networks = list((private.get("wifi") or {}).get("networks") or [])
    cloud=private.get('cloud') or {}
    cloud_ready=all(cloud.get(k) for k in ['base_url','user','password'])
    checks['wifi']={'ready':bool(networks),'state':'ready' if networks else 'missing',
                    'detail':f'已配置 {len(networks)} 组，首次安装按列表顺序连接' if networks else '请先填写至少一组 Wi-Fi 并应用'}
    checks['cloud']={'ready':cloud_ready,
                    'state':'configured' if cloud_ready else 'missing',
                    'detail':'凭据已填写；联网测试须主动执行，不代表设备已连通' if cloud_ready else
                             '请填写并应用 WebDAV 地址、用户名和应用密码；尚未完成云端配置'}
    checks['target']={'ready':(_FIRMWARE/'platformio.ini').is_file(),'state':'fixed',
                      'detail':'只支持 ESP32-S3 N32R16 与 SSD1677 800×480；不自动适配其他板'}
    ready=all(row['ready'] for row in checks.values())
    checks['network']={'ready':False,'state':'on_demand','required_for_build':False,
        'detail':'工具依赖只在点击准备时下载；云端测试须从运行详情主动执行，不自动调用付费模型'}
    checks['usb']={'ready':bool(_DEVICE_CACHE),'state':'detected' if _DEVICE_CACHE else 'physical_action',
        'required_for_build':False,'detail':'构建不要求MAC；连接数据线后可只读识别，再明确确认绑定'}
    return {
        "ready": ready,
        "target": TARGET,
        "chip": "ESP32-S3",
        "flash_size": "32MB",
        "screen": "4.26 英寸 800 x 480 SSD1677",
        "wifi_count": len(networks),
        "cloud_configured": checks['cloud']['ready'],
        "config_fingerprint": operator_config._public_fingerprint(public),
        "missing": [key for key,row in checks.items() if not row['ready'] and row.get('required_for_build',True)],
        'checks':checks,
        "writes_device": False,
    }


@critical_entry
def create_build(expected_confirmation: str | None = None) -> dict:
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
        "build_confirmation": expected_confirmation,
    }
    holder={}
    def insert(value):
        for existing in value.setdefault('builds',{}).values():
            if existing.get('status') in {'queued','running'} and all(existing.get(k)==job[k] for k in ['input_hash','source_hash']):
                if expected_confirmation is None or expected_confirmation == existing.get('build_confirmation'):
                    holder.update(existing);return
                raise RuntimeError('已有构建正在执行，请刷新状态')
        if any(row.get('status') in {'queued','running'} for row in value['builds'].values()):
            raise RuntimeError('已有构建正在执行，请等待结束')
        if expected_confirmation is not None and not hmac.compare_digest(expected_confirmation, _build_confirmation(input_hash,job['source_hash'],value)):
            raise RuntimeError('配置、工具或构建状态已变化，请重新检查并确认')
        value['builds'][task_id]=job;holder.update(job)
    _update(insert)
    return _public_job(holder)


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
    try:return _run_build_checked(task_id)
    except BuildCancelled as error:
        return _set('builds',task_id,status='cancelled',error=str(error))
    except Exception as error:
        return _set('builds',task_id,status='failed',error=str(error) if isinstance(error,RuntimeError) else '构建条件异常，原有配置与字库保留')


def _run_build_checked(task_id: str) -> dict:
    job = (_read().get("builds") or {}).get(task_id)
    if not isinstance(job, dict):
        raise KeyError(task_id)
    if job.get("status") == "completed":
        return _public_job(job)
    from . import firmware_setup
    with _BUILD_LOCK, _exclusive_lock("build"), firmware_setup.operation_lock(blocking=True):
        if _BUILD_CANCEL.get(task_id, threading.Event()).is_set():
            raise BuildCancelled('构建已取消，未写入设备')
        _set("builds", task_id, status="running", started_at=int(time.time()))
        _public, private, current_hash = _input_snapshot()
        if current_hash != job.get("input_hash"):
            return _set("builds", task_id, status="stale", error="配置已变化，请重新创建构建")
        if _source_hash() != job.get("source_hash"):
            return _set("builds", task_id, status="stale", error="固件源码已变化，请重新创建构建")
        private_dir = _PRIVATE / task_id
        private_dir.mkdir(parents=True, exist_ok=False)
        os.chmod(private_dir, 0o700)
        paths=firmware_setup.tool_paths()
        if not paths or not preflight()['ready']:
            return _set('builds',task_id,status='failed',error='准备条件已变化，请返回准备中心')
        project=private_dir/'project'
        shutil.copytree(_FIRMWARE,project,ignore=shutil.ignore_patterns('.pio','__pycache__','.git','fonts_gen','fonts_misans_16.h','fonts_misans_24.h'))
        fonts=firmware_setup.font_source()
        for name in firmware_setup.FONT_NAMES:
            destination=project/'src'/name;destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(fonts/name,destination)
        # Library payloads are private to this build. A build never downloads
        # a different dependency or modifies the signed/public source tree.
        shutil.copytree(paths['generation']/'project/.pio/libdeps',private_dir/'libdeps')
        env, redactions = _safe_environment(private)
        env["INKSIGHT_BUILD_ID"] = task_id
        env["PLATFORMIO_BUILD_DIR"] = str(private_dir / "build")
        env['PLATFORMIO_LIBDEPS_DIR']=str(private_dir/'libdeps')
        args = [str(paths['python']),str(_ROOT/'shared/tools/firmware_pio.py'),'run', '-d', str(project), '-e', TARGET]
        try:
            result = _run_build_command(args, timeout=20 * 60, env=env, cwd=_ROOT)
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
        if sources["boot_app0.bin"].resolve() != paths['boot_app0'].resolve():
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
        try:
            from .firmware_usb import validate_artifacts
            validate_artifacts({**job,'artifacts':artifacts,'private_dir':str(private_dir)})
        except RuntimeError as error:
            return _set('builds',task_id,status='failed',error=str(error),log=log)
        if _source_hash() != job.get("source_hash"):
            return _set("builds", task_id, status="stale", error="构建期间固件源码发生变化", log=log)
        if _BUILD_CANCEL.get(task_id, threading.Event()).is_set():
            raise BuildCancelled('构建已取消，未写入设备')
        return _set(
            "builds", task_id, status="completed", finished_at=int(time.time()),
            artifacts=artifacts, private_dir=str(private_dir), log=log,
        )


@critical_entry
def start_build(task_id: str) -> dict:
    with _THREADS_LOCK:
        existing = _THREADS.get(task_id)
        if not (existing and existing.is_alive()):
            job = get_build(task_id)
            if job.get("status") == "completed":
                return job
            _set("builds", task_id, status="queued")
            _BUILD_CANCEL[task_id] = threading.Event()
            worker = threading.Thread(target=run_build, args=(task_id,), daemon=True, name=f"inksight-{task_id}")
            _THREADS[task_id] = worker
            worker.start()
    return get_build(task_id)


def cancel_build(task_id: str) -> dict:
    job = get_build(task_id)
    if job['status'] in {'queued', 'running'}:
        _BUILD_CANCEL.setdefault(task_id, threading.Event()).set()
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
        from .firmware_setup import tool_paths
        paths=tool_paths()
        if not paths:return []
        script = (
            "import json; from serial.tools import list_ports; "
            "print(json.dumps([{'device':p.device,'description':p.description,'vid':p.vid,'pid':p.pid} "
            "for p in list_ports.comports()]))"
        )
        result = _run_command([str(paths['python']), "-c", script], timeout=15, cwd=_ROOT)
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


def _esptool(port: str, *args: str, after: str = 'no_reset') -> list[str]:
    from . import firmware_setup
    paths=firmware_setup.tool_paths()
    python,esptool=(paths['python'],paths['esptool']) if paths else (_PIO_PYTHON,_ESPTOOL)
    return [str(python), str(esptool), "--chip", CHIP, "--port", port, '--after',after, *args]


def _port_owner(port: str) -> str | None:
    result = _run_command(["/usr/sbin/lsof", "-Fn", "--", port], timeout=10, cwd=_ROOT)
    if result.returncode not in (0, 1):
        raise RuntimeError("无法确认串口占用状态")
    names = [line[1:] for line in (result.stdout or "").splitlines() if line.startswith("n")]
    return names[0] if names else None


def _identify(port: str) -> tuple[str, str]:
    if _port_owner(port):
        raise RuntimeError("串口正被其他程序占用，请先正常释放后重试")
    from .firmware_usb import mark_touched
    mark_touched()
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


@critical_entry
def probe_device(device_id: str) -> dict:
    """Explicit read-only identity before building; no need for an existing MAC."""
    from . import firmware_setup
    paths=firmware_setup.tool_paths()
    if not paths or not firmware_setup.tools_status()['ready']:
        raise RuntimeError('请先准备构建工具')
    device=_DEVICE_CACHE.get(device_id)
    if not device:raise RuntimeError('设备列表已变化，请重新查找')
    from .firmware_usb import usb_session, _resume, _SESSION
    with usb_session():
        try:
            try:mac,capacity=_identify(device['port'])
            finally:
                if getattr(_SESSION,'touched',False):_resume(device['port'])
        except subprocess.TimeoutExpired:raise RuntimeError('USB识别超时，请检查数据线或启动模式') from None
        ident=_new_id('probe');token=os.urandom(24).hex()
        row={'id':ident,'mac':mac.replace(':',''),'chip':'ESP32-S3','flash_size':capacity,
             'device_fingerprint':hashlib.sha256(mac.encode()).hexdigest()[:12],
             'confirmation_token':token,'expires_at':int(time.time())+300}
        _PROBE_CACHE.clear();_PROBE_CACHE[ident]=row
        return {key:value for key,value in row.items() if key!='mac'}


def consume_probe(ident: str, token: str) -> dict:
    with _THREADS_LOCK:
        row=_PROBE_CACHE.get(ident)
        if not row or row['expires_at']<=time.time() or not hmac.compare_digest(row['confirmation_token'],token):
            raise RuntimeError('设备绑定确认已过期，请重新只读识别')
        _PROBE_CACHE.pop(ident)
        return row


def _read_region(port: str, offset: str, size: str, path: Path, *, timeout: int = 180) -> bytes:
    try:
        result = _run_command(
            _esptool(port, "--baud", BAUD, "read_flash", offset, size, str(path)),
            timeout=timeout, cwd=_ROOT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"读取设备区域 {offset} 超时；本步骤未确认，请检查任务与备份记录") from exc
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


@critical_entry
def prepare_flash(build_id: str, device_id: str, install_mode: str = "update") -> dict:
    from .firmware_usb import prepare_sync
    return prepare_sync(build_id, device_id, install_mode)


def get_flash_plan_identity(plan_id: str) -> dict:
    plan = (_read().get("plans") or {}).get(plan_id)
    if not isinstance(plan, dict):
        raise KeyError(plan_id)
    return {"mac": str(plan.get("mac") or "").replace(':','').replace('-','').upper()}


def bind_previous_build(plan_id: str, heartbeat: dict | None) -> None:
    """Record an authenticated, recent pre-upgrade build for rollback proof."""
    if not isinstance(heartbeat, dict):
        return
    build_id = heartbeat.get("firmware_build_id")
    if not isinstance(build_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", build_id):
        return
    try:
        from datetime import datetime
        observed = int(datetime.fromisoformat(str(heartbeat.get("created_at") or "")).timestamp())
    except (TypeError, ValueError):
        return
    if not 0 <= int(time.time()) - observed <= 3600:
        return
    def update(value: dict) -> None:
        plan = value.setdefault("plans", {}).get(plan_id)
        if isinstance(plan, dict) and plan.get("status") == "ready":
            plan["previous_build_id"] = build_id
    _update(update)


@critical_entry
def create_flash(plan_id: str, confirmation_token: str) -> dict:
    flash_id = _new_id("flash")
    now = int(time.time())
    initial_plan = (_read().get("plans") or {}).get(plan_id)
    if not isinstance(initial_plan, dict):
        raise RuntimeError("flash plan is not ready")
    if initial_plan.get('backup_verified') is not True:
        raise RuntimeError('备份尚未完成并校验，不能写入')
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
            "previous_build_id": plan.get("previous_build_id"),
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
    from .firmware_usb import run_flash as guarded_flash
    with gate.work():
        try:return guarded_flash(flash_id)
        except Exception as error:
            message=str(error) if isinstance(error,RuntimeError) else '设备操作异常，停止自动操作并保留私有备份'
            return _set('flashes', flash_id, status='failed', error=message,needs_recovery=True)


@critical_entry
def start_flash(flash_id: str) -> dict:
    with _THREADS_LOCK:
        existing = _THREADS.get(flash_id)
        if not (existing and existing.is_alive()):
            job = (_read().get("flashes") or {}).get(flash_id)
            if not isinstance(job, dict):
                raise KeyError(flash_id)
            if job.get("status") != "queued":
                raise RuntimeError("此写入任务不可重复启动")
            _set("flashes", flash_id, status="queued")
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
    return {"mac": str(job.get("mac") or "").replace(':','').replace('-','').upper(), "build_id": str(job.get("build_id") or "")}


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
        canonical=lambda value:str(value or '').replace(':','').replace('-','').upper()
        if hmac.compare_digest(canonical(job.get('mac')),canonical(mac)) and hmac.compare_digest(
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
        network_verified=True,
        error=None,
    )


def _checked_backup(flash: dict, name: str) -> bytes:
    """Read an original region only when its recorded digest still matches."""
    expected = (flash.get("backup_manifest") or {}).get(name)
    path = _PRIVATE / str(flash["id"]) / "backup" / name
    if not isinstance(expected, str) or not path.is_file():
        raise RuntimeError("回退备份不完整；已拒绝写入")
    raw = path.read_bytes()
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected):
        raise RuntimeError("回退备份校验失败；已拒绝写入")
    return raw


def _checked_next_ota(flash: dict) -> bytes:
    path = _PRIVATE / str(flash["id"]) / "otadata-next.bin"
    expected = flash.get("next_ota_sha256")
    if not isinstance(expected, str) or not path.is_file():
        raise RuntimeError("更新后的启动项记录不完整；已拒绝回退")
    raw = path.read_bytes()
    if len(raw) != int(OTADATA_SIZE, 16) or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected):
        raise RuntimeError("更新后的启动项记录校验失败；已拒绝回退")
    return raw


@critical_entry
def prepare_rollback(flash_id: str, device_id: str) -> dict:
    from .firmware_usb import usb_session, _resume, _SESSION
    device=_DEVICE_CACHE.get(device_id)
    if not device:raise RuntimeError('设备列表已变化，请重新检测')
    with usb_session():
        try:return _prepare_rollback_checked(flash_id,device_id)
        finally:
            if getattr(_SESSION,'touched',False):_resume(str(device['port']))


def _prepare_rollback_checked(flash_id: str, device_id: str) -> dict:
    """Read-only device verification before offering an OTA-selector rollback."""
    flash = (_read().get("flashes") or {}).get(flash_id)
    device = _DEVICE_CACHE.get(device_id)
    if not isinstance(flash, dict) or flash.get("install_mode") != "update":
        raise RuntimeError("只有已有设备 OTA 更新支持此回退流程")
    if flash.get("status") not in {"completed", "network_timeout", "failed", "waiting_heartbeat"}:
        raise RuntimeError("更新尚未停止，不能准备回退")
    if not device:
        raise RuntimeError("设备列表已变化，请重新检测")
    original_ota = _checked_backup(flash, "otadata.bin")
    current_expected = _checked_next_ota(flash)
    if len(original_ota) != int(OTADATA_SIZE, 16):
        raise RuntimeError("原始启动项备份长度异常")
    _checked_backup(flash, "partition.bin")
    _checked_backup(flash, "nvs.bin")
    target_slot = flash.get("target_slot")
    if target_slot not in (0, 1):
        raise RuntimeError("原始更新目标分区未知")
    _checked_backup(flash, f"app{target_slot}.bin")
    previous_slot = 1 - target_slot
    previous_app = _checked_backup(flash, f"app{previous_slot}.bin")
    if len(previous_app) != int(APP_SIZE, 16) or previous_app[0] != 0xE9:
        raise RuntimeError("旧应用备份不完整，已拒绝回退")
    mac, flash_size = _identify(str(device["port"]))
    if not hmac.compare_digest(mac, str(flash.get("mac") or "")):
        raise RuntimeError("设备标识与原始更新记录不符")
    from .firmware_usb import _security
    _security(str(device['port']))
    plan_id = _new_id("rollback-plan")
    directory = _PRIVATE / plan_id
    directory.mkdir(parents=True, exist_ok=False)
    os.chmod(directory, 0o700)
    partition = _read_region(str(device["port"]), PARTITION_OFFSET, PARTITION_SIZE,
                             directory / "partition-current.bin")
    if not hmac.compare_digest(partition, _checked_backup(flash, "partition.bin")):
        raise RuntimeError("分区表已变化，不能自动回退")
    ota = _read_region(str(device["port"]), OTADATA_OFFSET, OTADATA_SIZE,
                       directory / "otadata-current.bin")
    if not hmac.compare_digest(ota, current_expected):
        raise RuntimeError("启动项已被其他更新改变，不能自动回退")
    previous_offset = APP1_OFFSET if previous_slot == 1 else APP_OFFSET
    current_previous_app = _read_region(str(device["port"]), previous_offset, APP_SIZE,
                                        directory / f"app{previous_slot}-current.bin", timeout=8 * 60)
    if not hmac.compare_digest(hashlib.sha256(current_previous_app).digest(),
                               hashlib.sha256(previous_app).digest()):
        raise RuntimeError("旧应用分区已变化，不能自动回退")
    token = os.urandom(24).hex()
    now = int(time.time())
    plan = {
        "id": plan_id, "flash_id": flash_id, "device_id": device_id,
        "status": "ready", "target": TARGET, "chip": "ESP32-S3",
        "flash_size": flash_size, "device_fingerprint": flash["device_fingerprint"],
        "port": device["port"], "mac": mac,
        "current_ota_sha256": hashlib.sha256(ota).hexdigest(),
        "restore_ota_sha256": hashlib.sha256(original_ota).hexdigest(),
        "confirmation_token": token, "created_at": now, "expires_at": now + 5 * 60,
        "scope": "仅恢复已校验的旧 OTA 启动项；不写 NVS、Wi-Fi、LittleFS、应用或分区表。设备联网仍需再次验证。",
    }
    _update(lambda value: value.setdefault("rollback_plans", {}).update({plan_id: plan}))
    public = _public_job(plan)
    public["confirmation_token"] = token
    return public


@critical_entry
def create_rollback(plan_id: str, confirmation_token: str) -> dict:
    rollback_id = _new_id("rollback")
    now = int(time.time())
    holder: dict = {}
    def consume(value: dict) -> None:
        plan = value.setdefault("rollback_plans", {}).get(plan_id)
        if not isinstance(plan, dict) or plan.get("status") != "ready":
            raise RuntimeError("回退计划不可用或已使用")
        if int(plan.get("expires_at") or 0) < now:
            raise RuntimeError("回退确认已过期")
        if not hmac.compare_digest(str(plan.get("confirmation_token") or ""), confirmation_token or ""):
            raise RuntimeError("回退确认码无效")
        plan["status"] = "consumed"
        job = {
            "id": rollback_id, "plan_id": plan_id, "flash_id": plan["flash_id"],
            "status": "queued", "target": TARGET, "port": plan["port"], "mac": plan["mac"],
            "device_fingerprint": plan["device_fingerprint"],
            "current_ota_sha256": plan["current_ota_sha256"],
            "restore_ota_sha256": plan["restore_ota_sha256"],
            "created_at": now, "updated_at": now, "verification": "pending",
        }
        value.setdefault("rollbacks", {})[rollback_id] = job
        holder.update(job)
    _update(consume)
    return _public_job(holder)


def run_rollback(rollback_id: str) -> dict:
    with gate.work():
        try:return _run_rollback_checked(rollback_id)
        except Exception as error:
            message=str(error) if isinstance(error,RuntimeError) else '回退未能安全完成，请保留备份并重新检查设备'
            return _set('rollbacks',rollback_id,status='failed',error=message,needs_recovery=True)


def _run_rollback_checked(rollback_id: str) -> dict:
    job = (_read().get("rollbacks") or {}).get(rollback_id)
    if not isinstance(job, dict):
        raise KeyError(rollback_id)
    from .firmware_usb import usb_session, _SESSION, _security
    with usb_session():
        _set("rollbacks", rollback_id, status="running", started_at=int(time.time()))
        directory = _PRIVATE / rollback_id
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        logs: list[str] = []
        selector_attempted=False
        try:
            flash = (_read().get("flashes") or {}).get(job.get("flash_id"))
            if not isinstance(flash, dict) or flash.get("install_mode") != "update":
                raise RuntimeError("原始更新任务不可用")
            original_ota = _checked_backup(flash, "otadata.bin")
            expected_next = _checked_next_ota(flash)
            if not hmac.compare_digest(hashlib.sha256(original_ota).hexdigest(),
                                       str(job.get("restore_ota_sha256") or "")):
                raise RuntimeError("回退备份与确认时不一致")
            if not hmac.compare_digest(hashlib.sha256(expected_next).hexdigest(),
                                       str(job.get("current_ota_sha256") or "")):
                raise RuntimeError("当前启动项与确认时不一致")
            mac, _flash_size = _identify(str(job["port"]))
            if not hmac.compare_digest(mac, str(job["mac"])):
                raise RuntimeError("执行前检测到设备已更换")
            _security(str(job['port']))
            partition = _read_region(str(job["port"]), PARTITION_OFFSET, PARTITION_SIZE,
                                     directory / "partition-before.bin")
            if not hmac.compare_digest(partition, _checked_backup(flash, "partition.bin")):
                raise RuntimeError("分区表已变化；未执行回退")
            current = _read_region(str(job["port"]), OTADATA_OFFSET, OTADATA_SIZE,
                                   directory / "otadata-before.bin")
            if not hmac.compare_digest(current, expected_next):
                raise RuntimeError("启动项已变化；未执行回退")
            target_slot = flash.get("target_slot")
            if target_slot not in (0, 1):
                raise RuntimeError("旧应用分区未知；未执行回退")
            previous_slot = 1 - target_slot
            previous_app = _checked_backup(flash, f"app{previous_slot}.bin")
            previous_offset = APP1_OFFSET if previous_slot == 1 else APP_OFFSET
            live_app = _read_region(str(job["port"]), previous_offset, APP_SIZE,
                                    directory / "previous-app-before.bin", timeout=8 * 60)
            if not hmac.compare_digest(hashlib.sha256(live_app).digest(),
                                       hashlib.sha256(previous_app).digest()):
                raise RuntimeError("旧应用分区已变化；未执行回退")
            nvs_before = _read_region(str(job["port"]), NVS_OFFSET, NVS_SIZE,
                                      directory / "nvs-before.bin")
            # Restore only the original OTA selector; the previous application
            # has not been overwritten by this update, and NVS is never written.
            from .firmware_usb import _selector_sector
            changed_sector=_selector_sector(original_ota,expected_next)
            original_path=directory/'restore-selector-sector.bin'
            original_path.write_bytes(original_ota[changed_sector:changed_sector+4096])
            restore_offset=hex(int(OTADATA_OFFSET,16)+changed_sector)
            base = _esptool(str(job["port"]))
            selector_attempted=True
            _step(base + ["--baud", BAUD, "write_flash", restore_offset, str(original_path)],
                  logs, "恢复旧启动项")
            _step(base + ["verify_flash", restore_offset, str(original_path)],
                  logs, "校验旧启动项")
            actual_ota=_read_region(str(job['port']),OTADATA_OFFSET,OTADATA_SIZE,directory/'otadata-after.bin')
            if actual_ota!=original_ota:raise RuntimeError('回退启动项完整回读不符，暂不重启')
            nvs_after = _read_region(str(job["port"]), NVS_OFFSET, NVS_SIZE,
                                     directory / "nvs-after.bin")
            if not hmac.compare_digest(nvs_before, nvs_after):
                raise RuntimeError("回退期间 NVS 发生变化；已停止自动重启")
            verified_at = int(time.time())
            _set("rollbacks", rollback_id, status="written_unconfirmed",
                 verified_at=verified_at, verification="ota-selector-verified; rebooting",
                 log="\n".join(logs)[-12000:])
            _step(base + ["run"], logs, "重启设备")
            return _set("rollbacks", rollback_id, status="written_unconfirmed",
                        verified_at=verified_at, verification="ota-selector-verified; rebooted",
                        log="\n".join(logs)[-12000:])
        except Exception as exc:
            recovery=False
            try:
                from .firmware_usb import _resume
                if not selector_attempted:
                    if getattr(_SESSION,'touched',False):_resume(str(job['port']))
                else:
                    recovery=True
                    rescue=directory/'restore-prior-selector-sector.bin'
                    rescue.write_bytes(expected_next[changed_sector:changed_sector+4096])
                    _step(base+['write_flash',restore_offset,str(rescue)],logs,'恢复回退前启动项')
                    _step(base+['verify_flash',restore_offset,str(rescue)],logs,'校验回退前启动项')
                    if _read_region(str(job['port']),NVS_OFFSET,NVS_SIZE,directory/'nvs-rescue.bin')!=nvs_before:
                        raise RuntimeError('回退期间设置改变，停止自动重启')
                    _resume(str(job['port']));recovery=False
            except Exception:recovery=True
            message=str(exc) if isinstance(exc,RuntimeError) else '回退发生异常，已停止；请核对设备与私有备份'
            return _set("rollbacks", rollback_id, status="failed", error=message,
                        needs_recovery=recovery,
                        log="\n".join(logs)[-12000:])


@critical_entry
def start_rollback(rollback_id: str) -> dict:
    with _THREADS_LOCK:
        existing = _THREADS.get(rollback_id)
        if not (existing and existing.is_alive()):
            job = (_read().get("rollbacks") or {}).get(rollback_id)
            if not isinstance(job, dict):
                raise KeyError(rollback_id)
            if job.get("status") != "queued":
                raise RuntimeError("此回退任务不可重复启动")
            _set("rollbacks", rollback_id, status="queued")
            worker = threading.Thread(target=run_rollback, args=(rollback_id,), daemon=True,
                                      name=f"inksight-{rollback_id}")
            _THREADS[rollback_id] = worker
            worker.start()
    return get_rollback(rollback_id)


def get_rollback(rollback_id: str) -> dict:
    job = (_read().get("rollbacks") or {}).get(rollback_id)
    if not isinstance(job, dict):
        raise KeyError(rollback_id)
    if job.get("status") == "running":
        with _THREADS_LOCK:
            worker = _THREADS.get(rollback_id)
            if not (worker and worker.is_alive()):
                return _set("rollbacks", rollback_id, status="failed",
                            error="回退任务异常结束；请检查设备和私有备份")
    return _public_job(job)


def get_rollback_identity(rollback_id: str) -> dict:
    job = (_read().get("rollbacks") or {}).get(rollback_id)
    if not isinstance(job, dict):
        raise KeyError(rollback_id)
    flash = (_read().get("flashes") or {}).get(job.get("flash_id")) or {}
    return {"mac": str(job.get("mac") or "").replace(':','').replace('-','').upper(),
            "previous_build_id": str(flash.get("previous_build_id") or "")}


def confirm_rollback_heartbeat(rollback_id: str, heartbeat: dict | None) -> dict:
    job = (_read().get("rollbacks") or {}).get(rollback_id)
    if not isinstance(job, dict):
        raise KeyError(rollback_id)
    if job.get("status") != "written_unconfirmed" or not isinstance(heartbeat, dict):
        return _public_job(job)
    expected = get_rollback_identity(rollback_id)["previous_build_id"]
    if not expected or not hmac.compare_digest(expected, str(heartbeat.get("firmware_build_id") or "")):
        return _public_job(job)
    try:
        from datetime import datetime
        observed = int(datetime.fromisoformat(str(heartbeat.get("created_at") or "")).timestamp())
    except (TypeError, ValueError):
        return _public_job(job)
    if observed <= int(job.get("verified_at") or 0):
        return _public_job(job)
    return _set("rollbacks", rollback_id, status="completed",
                finished_at=int(time.time()), verification="ota-selector-verified; matching-old-build-heartbeat")


def recover_interrupted_jobs():
    """Startup only, before accepting jobs. Never retry a write automatically."""
    def update(data):
        for group in ['builds','plans','flashes','rollback_plans','rollbacks']:
            for ident,row in data.get(group,{}).items():
                if row.get('status') in {'queued','running'} and ident not in _THREADS:
                    row.update(status='interrupted',error='上次任务中断，请重新核对；不会自动重试USB写入')
    _update(update)


def expire_verification_deadlines():
    now=time.time()
    if not any(job.get('status')=='waiting_heartbeat' and job.get('heartbeat_deadline',now+1)<now
               for job in _read().get('flashes',{}).values()):return
    def update(data):
        for job in data.get('flashes',{}).values():
            if job.get('status')=='waiting_heartbeat' and job.get('heartbeat_deadline',now+1)<now:
                job.update(status='network_timeout',error='写入已校验，尚未取得匹配的设备联网回执')
    _update(update)
