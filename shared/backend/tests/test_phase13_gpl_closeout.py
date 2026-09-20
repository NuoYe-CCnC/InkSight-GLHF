from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _load(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_top_level_license_is_exact_gplv3_text_and_scope_is_explicit():
    assert (ROOT / "LICENSE").read_bytes() == (
        ROOT / "LICENSES/GxEPD2-1.6.9-GPL-3.0.txt"
    ).read_bytes()
    scope = (ROOT / "LICENSE-SCOPE.md").read_text(encoding="utf-8")
    assert "GPL-3.0-only" in scope
    assert "third-party" in scope.lower()
    assert "does not" in scope.lower()


def test_python39_macos_arm64_lock_is_hashed_and_path_independent():
    lock = (ROOT / "requirements-py39-macos-arm64.lock").read_text(encoding="utf-8")
    packages = re.findall(r"(?m)^([A-Za-z0-9_.-]+)==([^ \\\n]+)", lock)
    hashes = re.findall(r"--hash=sha256:[0-9a-f]{64}", lock)
    assert len(packages) >= 80
    assert len(hashes) >= len(packages)
    assert "/Users/" not in lock
    assert "file://" not in lock


def test_sbom_declares_project_license_and_verified_lock_baseline():
    sbom = json.loads((ROOT / "SBOM.json").read_text(encoding="utf-8"))
    assert sbom["project_license"] == "GPL-3.0-only"
    assert sbom["python"]["hash_locked"] is True
    assert sbom["python"]["verified_lock"] == "requirements-py39-macos-arm64.lock"
    assert sbom["python"]["verified_platform"] == "macOS ARM64"
    assert sbom["python"]["verified_python"] == "3.9"


def test_release_builder_includes_phase13_material_and_current_gate_is_open():
    module = _load("shared/tools/release/build_candidate.py", "build_candidate_phase13")
    collected = {path.relative_to(ROOT).as_posix() for path in module.collect(ROOT)}
    for name in (
        "LICENSE",
        "LICENSE-SCOPE.md",
        "requirements-py39-macos-arm64.lock",
        "docs/phase13-gpl-release-closeout-2026-09-14/01-implementation-plan.md",
    ):
        assert name in collected
    source = (ROOT / "shared/tools/release/build_candidate.py").read_text(encoding="utf-8")
    assert "PUBLICATION_READY = True" in source
    assert '"publication_ready": PUBLICATION_READY' in source
    assert "top-level project license not selected" not in source


def test_phase13_documents_keep_unresolved_items_visible():
    gate = (ROOT / "docs/phase13-gpl-release-closeout-2026-09-14/08-publish-gates-and-owner-confirmations.md").read_text(encoding="utf-8")
    assert "后续新增内容" in gate
    assert "CC0" in gate
    assert "TechCrunch" in gate
    assert "The Verge" in gate
    assert "禁止发布" in gate
