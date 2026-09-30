"""Offline simulation of guarded OTA rollback; no serial port is accessed."""
from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import Path
import subprocess
import time

import pytest

from core import firmware_tasks as fw
from core import state_store


@pytest.fixture
def rollback_lab(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir()
    monkeypatch.setattr(fw, "_PRIVATE", private)
    monkeypatch.setattr(fw, "_STATE", tmp_path / "tasks.json")
    monkeypatch.setattr(fw, "_DEVICE_CACHE", {"usb-test": {"port": "/dev/cu.test", "description": "test"}})
    monkeypatch.setattr(fw, "_identify", lambda _port: ("AA:BB:CC:DD:EE:FF", "32MB"))

    flash_id = "flash-test"
    backup = private / flash_id / "backup"
    backup.mkdir(parents=True)
    partition = b"P" * int(fw.PARTITION_SIZE, 16)
    nvs = b"N" * int(fw.NVS_SIZE, 16)
    original = b"\xff" * int(fw.OTADATA_SIZE, 16)
    next_ota, target_slot = fw._next_ota_data(original)
    previous_slot = 1 - target_slot
    previous_app = b"\xe9" + b"\xff" * (int(fw.APP_SIZE, 16) - 1)
    files = {"partition.bin": partition, "nvs.bin": nvs,
             "otadata.bin": original, f"app{target_slot}.bin": b"old-inactive-app",
             f"app{previous_slot}.bin": previous_app}
    for name, raw in files.items():
        (backup / name).write_bytes(raw)
    (backup.parent / "otadata-next.bin").write_bytes(next_ota)
    flash = {
        "id": flash_id, "status": "completed", "install_mode": "update",
        "mac": "AA:BB:CC:DD:EE:FF", "device_fingerprint": "test-fingerprint",
        "previous_build_id": "old-build", "target_slot": target_slot,
        "backup_manifest": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        "next_ota_sha256": hashlib.sha256(next_ota).hexdigest(),
    }
    state_store.write_json(fw._STATE, {"builds": {}, "plans": {}, "flashes": {flash_id: flash},
                                       "rollback_plans": {}, "rollbacks": {}}, meta=False)
    previous_offset = fw.APP1_OFFSET if previous_slot == 1 else fw.APP_OFFSET
    device = {fw.PARTITION_OFFSET: partition, fw.NVS_OFFSET: nvs,
              fw.OTADATA_OFFSET: next_ota, previous_offset: previous_app}
    writes = []

    def read_region(_port, offset, size, path, **_kwargs):
        raw = device[offset]
        assert len(raw) == int(size, 16)
        Path(path).write_bytes(raw)
        return raw

    def command(args, **_kwargs):
        if "write_flash" in args:
            index = args.index("write_flash")
            offset, path = args[index + 1:index + 3]
            device[offset] = Path(path).read_bytes()
            writes.append(offset)
        if "verify_flash" in args:
            index = args.index("verify_flash")
            offset, path = args[index + 1:index + 3]
            assert device[offset] == Path(path).read_bytes()
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(fw, "_read_region", read_region)
    monkeypatch.setattr(fw, "_run_command", command)
    return flash_id, backup, device, writes, original, next_ota


def test_plan_is_read_only_and_rollback_writes_only_ota(rollback_lab):
    flash_id, _backup, device, writes, original, next_ota = rollback_lab
    plan = fw.prepare_rollback(flash_id, "usb-test")
    assert writes == [] and device[fw.OTADATA_OFFSET] == next_ota
    with pytest.raises(RuntimeError, match="确认码"):
        fw.create_rollback(plan["id"], "wrong")
    task = fw.create_rollback(plan["id"], plan["confirmation_token"])
    with pytest.raises(RuntimeError, match="已使用"):
        fw.create_rollback(plan["id"], plan["confirmation_token"])
    result = fw.run_rollback(task["id"])
    assert result["status"] == "written_unconfirmed"
    assert writes == [fw.OTADATA_OFFSET]
    assert device[fw.OTADATA_OFFSET] == original
    assert device[fw.NVS_OFFSET] == b"N" * int(fw.NVS_SIZE, 16)
    heartbeat = {"firmware_build_id": "old-build",
                 "created_at": datetime.fromtimestamp(int(time.time()) + 2).isoformat()}
    assert fw.confirm_rollback_heartbeat(task["id"], heartbeat)["status"] == "completed"


def test_changed_ota_or_corrupt_backup_fails_before_writing(rollback_lab):
    flash_id, backup, device, writes, _original, _next_ota = rollback_lab
    device[fw.OTADATA_OFFSET] = b"X" * int(fw.OTADATA_SIZE, 16)
    with pytest.raises(RuntimeError, match="启动项已被其他更新改变"):
        fw.prepare_rollback(flash_id, "usb-test")
    assert writes == []
    (backup / "otadata.bin").write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="备份校验失败"):
        fw.prepare_rollback(flash_id, "usb-test")
    assert writes == []


def test_changed_previous_application_fails_before_writing(rollback_lab):
    flash_id, _backup, device, writes, _original, _next_ota = rollback_lab
    device[fw.APP_OFFSET] = b"\xe9" + b"Z" * (int(fw.APP_SIZE, 16) - 1)
    with pytest.raises(RuntimeError, match="旧应用分区已变化"):
        fw.prepare_rollback(flash_id, "usb-test")
    assert writes == []


def test_device_change_after_confirmation_is_rejected(rollback_lab):
    flash_id, _backup, device, writes, _original, _next_ota = rollback_lab
    plan = fw.prepare_rollback(flash_id, "usb-test")
    task = fw.create_rollback(plan["id"], plan["confirmation_token"])
    device[fw.OTADATA_OFFSET] = b"Q" * int(fw.OTADATA_SIZE, 16)
    result = fw.run_rollback(task["id"])
    assert result["status"] == "failed"
    assert writes == []
