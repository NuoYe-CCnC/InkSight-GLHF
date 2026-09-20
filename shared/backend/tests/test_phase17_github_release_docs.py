from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PUBLIC_DOCS = (
    "README.md", "README.en.md", "CONTRIBUTING.md", "SECURITY.md",
    "docs/INSTALLATION.zh-CN.md", "docs/INSTALLATION.en.md",
    "docs/RELEASE_NOTES_DRAFT.zh-CN.md", "docs/RELEASE_NOTES_DRAFT.en.md",
    "docs/SUPPORT_MATRIX.md", "docs/GITHUB_RELEASE_CHECKLIST.zh-CN.md",
)


def _load_builder():
    path = ROOT / "shared/tools/release/build_candidate.py"
    spec = importlib.util.spec_from_file_location("build_candidate_phase17", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_public_release_documents_are_in_candidate_allowlist():
    collected = {path.relative_to(ROOT).as_posix() for path in _load_builder().collect(ROOT)}
    for relative in PUBLIC_DOCS:
        assert relative in collected
    for relative in (
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        "shared/backend/tests/test_phase17_github_release_docs.py",
    ):
        assert relative in collected


def test_markdown_relative_links_resolve_and_docs_have_no_private_absolute_paths():
    link_re = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for relative in PUBLIC_DOCS:
        path = ROOT / relative
        text = path.read_text(encoding="utf-8")
        assert "/Users/" not in text and "/home/" not in text
        for target in link_re.findall(text):
            value = target.strip().split("#", 1)[0]
            if not value or value.startswith(("http://", "https://", "mailto:")):
                continue
            assert (path.parent / value).resolve().exists(), f"broken link: {relative} -> {target}"


def test_chinese_and_english_readmes_keep_required_release_boundaries():
    zh = (ROOT / "README.md").read_text(encoding="utf-8")
    en = (ROOT / "README.en.md").read_text(encoding="utf-8")
    for text in (zh, en):
        assert "InkSight-GLHF" in text
        assert "epd_426_ssd1677_s3_n32r16" in text
        assert "GPL-3.0-only" in text
        assert "2000 mAh" in text
        assert "MiSans" in text
    assert "远程新闻源全部关闭" in zh and "UTC 自然月" in zh
    assert "All remote news sources are disabled" in en and "UTC calendar month" in en
    assert "not an official product" in en


def test_documented_paths_exist_in_the_release_tree():
    for relative in (
        "requirements-py39-macos-arm64.lock",
        "shared/backend/run-backend.sh",
        "shared/tools/inksight_config.py",
        "shared/tools/import_misans.py",
        "shared/tools/admin_recovery.py",
        "shared/firmware/platformio.ini",
    ):
        assert (ROOT / relative).is_file()
    ini = (ROOT / "shared/firmware/platformio.ini").read_text(encoding="utf-8")
    assert "[env:epd_426_ssd1677_s3_n32r16]" in ini


def test_release_defaults_and_four_message_license_match_docs():
    config = json.loads((ROOT / "shared/config/inksight_config.example.json").read_text(encoding="utf-8"))
    news = config["news_digest"]
    assert news["sources"] == []
    assert not any(news["source_enabled"].values())
    assert config["panel_display"]["show_codex_credits"] is False
    assert config["panel_display"]["show_openai_api_info"] is False
    messages = json.loads((ROOT / "docs/phase14-content-source-release-2026-09-14/daily-messages-replacement-draft.json").read_text(encoding="utf-8"))
    assert messages["license"] == "GPL-3.0-only"
    assert len(messages["messages"]) == 4


def test_security_and_issue_templates_use_the_final_private_channel():
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "NuoYe-CCnC/InkSight-GLHF/security/advisories/new" in security
    assert "SECURITY_CONTACT_EMAIL_TO_BE_SET" not in security
    assert "response-time" in security
    bug = (ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml").read_text(encoding="utf-8")
    assert "real MAC" in bug and "SECURITY.md" in bug


def test_release_notes_preserve_backend_api_version_boundary():
    zh = (ROOT / "docs/RELEASE_NOTES_DRAFT.zh-CN.md").read_text(encoding="utf-8")
    en = (ROOT / "docs/RELEASE_NOTES_DRAFT.en.md").read_text(encoding="utf-8")
    assert "1.1.0" in zh and "不是项目发布版本号" in zh
    assert "1.1.0" in en and "not the project release version" in en
