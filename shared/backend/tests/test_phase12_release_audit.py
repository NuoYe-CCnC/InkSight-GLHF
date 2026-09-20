from __future__ import annotations

import importlib.util
import json
import stat
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _load(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_release_metadata_and_upstream_notice_are_present():
    sbom = json.loads((ROOT / "SBOM.json").read_text(encoding="utf-8"))
    assert len(sbom["components"]) >= 80
    assert any(item["name"] == "GxEPD2" and item["spdx"] == "GPL-3.0-only"
               for item in sbom["components"])
    assert (ROOT / "LICENSES/InkSight-upstream-MIT.txt").is_file()
    assert (ROOT / "LICENSE").is_file()


def test_public_font_setup_does_not_fetch_unlicensed_bitmap_mirror():
    module = _load("shared/backend/scripts/setup_fonts.py", "setup_fonts_phase12")
    assert module.BITMAP_FONT_URLS == {}
    source = (ROOT / "shared/backend/scripts/setup_fonts.py").read_text(encoding="utf-8")
    assert "raw.githubusercontent.com/carrothu-cn" not in source


def test_first_supported_firmware_dependencies_are_exact():
    ini = (ROOT / "shared/firmware/platformio.ini").read_text(encoding="utf-8")
    block = ini.split("[env:epd_426_ssd1677_s3_n32r16]", 1)[1].split("[env:", 1)[0]
    for pin in (
        "GxEPD2@1.6.9", "Adafruit GFX Library@1.12.6", "Adafruit BusIO@1.17.4",
        "WebSockets@2.7.3", "ArduinoJson@7.4.3", "NimBLE-Arduino@2.5.1",
    ):
        assert pin in block
    assert "@^" not in block


def test_candidate_build_is_sealed_and_fresh_extract_verified(tmp_path):
    module = _load("shared/tools/release/build_candidate.py", "build_candidate_phase12")
    destination = tmp_path / "InkSight-Source"
    report = module.build(ROOT, destination)
    assert report["findings"] == []
    assert report["verification"]["fresh_extract_matches"] is True
    assert report["verification"]["privacy_findings"] == 0
    archive = destination.with_suffix(".zip")
    receipt = json.loads(archive.with_suffix(".zip.receipt.json").read_text(encoding="utf-8"))
    assert receipt["archive_sha256"] == report["archive_sha256"]
    assert receipt["publication_ready"] is True
    assert receipt["blockers"] == []
    assert (destination / "PUBLICATION-READY.md").is_file()
    assert not (destination / "PUBLICATION-BLOCKED.md").exists()
    assert not list(destination.rglob(".pytest_cache"))
    with zipfile.ZipFile(archive) as bundle:
        script = bundle.getinfo("InkSight-Source/shared/backend/run-backend.sh")
        assert stat.S_IMODE(script.external_attr >> 16) & stat.S_IXUSR


def test_archive_verifier_rejects_path_traversal(tmp_path):
    module = _load("shared/tools/release/build_candidate.py", "verify_candidate_phase12")
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("InkSight-Source/../escape.txt", "x")
    with pytest.raises(ValueError, match="不安全路径"):
        module.verify_archive(archive, "InkSight-Source", [])


def test_personal_member_date_is_not_hardcoded_in_public_runtime_comments():
    targets = [
        ROOT / "shared/backend/core/feed_document.py",
        ROOT / "shared/backend/core/json_content.py",
        ROOT / "shared/firmware/src/panel_pages.cpp",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in targets)
    private_date = "2026-09" + "-23"
    assert private_date not in combined
    private_label = "有效至 09" + "-23"
    assert private_label not in combined


def test_offline_startup_switch_is_documented_and_gates_pollers():
    env = (ROOT / "shared/backend/.env.example").read_text(encoding="utf-8")
    source = (ROOT / "shared/backend/api/shared.py").read_text(encoding="utf-8")
    assert "INKSIGHT_OFFLINE_STARTUP=0" in env
    assert 'os.getenv("INKSIGHT_OFFLINE_STARTUP"' in source
    assert "if not offline_startup:" in source
