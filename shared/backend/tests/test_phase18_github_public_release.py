from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
REPO_URL = "https://github.com/NuoYe-CCnC/InkSight-GLHF"
VERSION = "v0.1.0-test.1"


def _load_builder():
    path = ROOT / "shared/tools/release/build_candidate.py"
    spec = importlib.util.spec_from_file_location("build_candidate_phase18", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_final_release_documents_are_allowlisted():
    collected = {path.relative_to(ROOT).as_posix() for path in _load_builder().collect(ROOT)}
    for relative in (
        "docs/releases/v0.1.0-test.1.zh-CN.md",
        "docs/releases/v0.1.0-test.1.en.md",
        "shared/backend/tests/test_phase18_github_public_release.py",
        "docs/phase18-github-public-test-release-2026-09-20/01-publication-scope.md",
        "docs/phase18-github-public-test-release-2026-09-20/02-publication-record.md",
    ):
        assert relative in collected


def test_public_entrypoints_use_final_repository_and_version():
    for relative in ("README.md", "README.en.md", "SECURITY.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert REPO_URL in text
        assert VERSION in text
        assert "<OWNER>" not in text


def test_release_notes_preserve_test_and_acceptance_boundaries():
    zh = (ROOT / "docs/releases/v0.1.0-test.1.zh-CN.md").read_text(encoding="utf-8")
    en = (ROOT / "docs/releases/v0.1.0-test.1.en.md").read_text(encoding="utf-8")
    assert "公开测试版" in zh and "pre-release" in zh
    assert "public test release" in en and "pre-release" in en
    assert "跳过" in zh and "下载" in zh and "安装" in zh and "运行" in zh
    assert "skips" in en and "download" in en and "install" in en and "run" in en
    for text in (zh, en):
        assert "2000 mAh" in text
        assert "MiSans" in text
        assert REPO_URL in text


def test_no_release_placeholders_remain_in_current_public_docs():
    for relative in (
        "README.md", "README.en.md", "SECURITY.md",
        "docs/releases/v0.1.0-test.1.zh-CN.md",
        "docs/releases/v0.1.0-test.1.en.md",
        "docs/GITHUB_RELEASE_CHECKLIST.zh-CN.md",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "<OWNER>" not in text
        assert "SECURITY_CONTACT_EMAIL_TO_BE_SET" not in text
