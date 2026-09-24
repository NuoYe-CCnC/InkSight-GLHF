#!/usr/bin/env python3
"""Build and independently verify a privacy-scanned InkSight source archive."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


ROOT_FILES = (
    ".gitignore", "LICENSE", "LICENSE-SCOPE.md", "README.md", "README.en.md",
    "CONTRIBUTING.md", "SECURITY.md", "SBOM.json", "THIRD_PARTY_NOTICES.md", "requirements.lock.txt",
    "requirements-py39-macos-arm64.lock",
)
EXACT_FILES = (
    "shared/backend/.env.example", "shared/backend/pytest.ini",
    "shared/backend/requirements.txt", "shared/backend/run-backend.sh",
    "shared/backend/run-backend.bat", "shared/backend/scripts/build_native_dither.py",
    "shared/backend/scripts/setup_fonts.py", "shared/backend/tests/conftest.py",
    "shared/backend/tests/test_phase11_release_prep.py",
    "shared/backend/tests/test_phase12_release_audit.py",
    "shared/backend/tests/test_phase13_gpl_closeout.py", "shared/backend/data/daily_messages.json",
    "shared/backend/tests/test_phase14_source_policy.py",
    "shared/backend/tests/test_phase15_daily_message_providers.py",
    "shared/backend/tests/test_phase16_release_overlay.py",
    "shared/backend/tests/test_phase17_github_release_docs.py",
    "shared/backend/data/news_calendar.json", "shared/config/README.md",
    "shared/config/inksight_config.example.json", "shared/config/inksight_secrets.example.json",
    "shared/config/manual_settings.example.json", "shared/firmware/fonts_full_charset.txt",
    "shared/firmware/data/portal_html.h", "shared/firmware/merge_firmware.py",
    "shared/firmware/platformio.ini", "shared/tools/admin_recovery.py",
    "shared/tools/font_gen_ttf.py", "shared/tools/import_misans.py",
    "shared/tools/misans_panel_render.py",
    "shared/tools/inksight_config.py", "shared/tools/misans_raster.py",
    "shared/tools/release/build_candidate.py", "shared/tools/release/generate_release_metadata.py",
    ".github/ISSUE_TEMPLATE/bug_report.yml", ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/config.yml", "docs/INSTALLATION.zh-CN.md",
    "docs/INSTALLATION.en.md", "docs/RELEASE_NOTES_DRAFT.zh-CN.md",
    "docs/RELEASE_NOTES_DRAFT.en.md", "docs/SUPPORT_MATRIX.md",
    "docs/GITHUB_RELEASE_CHECKLIST.zh-CN.md",
    "docs/releases/v0.1.0-test.1.zh-CN.md", "docs/releases/v0.1.0-test.1.en.md",
    "shared/backend/tests/test_phase18_github_public_release.py",
    "shared/backend/tests/test_deepseek_activity_attribution.py",
    "shared/backend/tests/test_phase20_token_activity.py",
)
PREFIXES = (
    "LICENSES", "shared/backend/api", "shared/backend/core", "shared/backend/migrations",
    "shared/backend/static/console", "shared/backend/static/manager", "shared/firmware/partitions",
    "shared/firmware/src", "docs/phase11-open-source-release-prep-2026-09-14",
    "docs/phase12-release-license-audit-2026-09-14",
    "docs/phase13-gpl-release-closeout-2026-09-14",
    "docs/phase14-content-source-release-2026-09-14",
    "docs/phase15-daily-message-provider-audit-2026-09-14",
    "docs/phase16-approved-daily-message-release-2026-09-14",
    "docs/phase17-github-test-release-prep-2026-09-20",
    "docs/phase18-github-public-test-release-2026-09-20",
    "docs/phase19-news-activity-attribution-2026-09-23",
    "docs/phase20-token-and-activity-2026-09-24",
)
DAILY_MESSAGE_TARGET = Path("shared/backend/data/daily_messages.json")
DAILY_MESSAGE_RELEASE_SOURCE = Path(
    "docs/phase14-content-source-release-2026-09-14/daily-messages-replacement-draft.json"
)
PUBLICATION_READY = True
TEXT_SUFFIXES = {
    ".py", ".json", ".html", ".css", ".js", ".cpp", ".h", ".ini",
    ".csv", ".md", ".txt", ".sh", ".bat",
}
FORBIDDEN_PARTS = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git", "state",
    "runtime_uploads", "uploads", "logs", "backups", ".venv", ".pio", ".local", "custom",
}
FORBIDDEN_NAMES = {
    ".DS_Store", ".env", ".jwt_secret", "mobile_note.json", "fonts_misans_16.h",
    "fonts_misans_24.h", "libeink_dither.so",
}
FORBIDDEN_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".otf", ".ttf", ".ttc", ".pcf", ".bin", ".png",
    ".jpg", ".jpeg", ".webp", ".so", ".dylib", ".dll", ".pyc", ".zip", ".tar",
    ".gz", ".tgz", ".7z", ".rar",
}
CONTENT_RULES = {
    "absolute macOS user path": re.compile(rb"/Users/[A-Za-z0-9._-]+/"),
    "absolute Linux user path": re.compile(rb"/home/[A-Za-z0-9._-]+/"),
    "WeChat identifier": re.compile(rb"wxid_[A-Za-z0-9_]+"),
    "private key block": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "probable OpenAI-style key": re.compile(rb"\bsk-[A-Za-z0-9_-]{16,}\b"),
    "probable GitHub token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "probable AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "probable Google API key": re.compile(rb"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "probable JWT": re.compile(rb"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\b"),
    "credential in URL": re.compile(rb"https?://[^\s/:]+:[^\s/@]+@"),
    "literal MAC address": re.compile(rb"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b"),
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _allowed(source: Path, root: Path) -> bool:
    rel_path = source.relative_to(root)
    rel = rel_path.as_posix()
    if source.is_symlink() or set(rel_path.parts) & FORBIDDEN_PARTS:
        return False
    if source.name in FORBIDDEN_NAMES or source.suffix.lower() in FORBIDDEN_SUFFIXES:
        return False
    if source.name.startswith("misans_") and source.suffix == ".h":
        return False
    if rel in ROOT_FILES or rel in EXACT_FILES:
        return True
    return source.suffix.lower() in TEXT_SUFFIXES and any(
        rel == prefix or rel.startswith(prefix + "/") for prefix in PREFIXES
    )


def collect(root: Path) -> list[Path]:
    candidates: set[Path] = set()
    for name in ROOT_FILES + EXACT_FILES:
        path = root / name
        if path.is_file():
            candidates.add(path)
    for prefix in PREFIXES:
        base = root / prefix
        if base.is_dir():
            candidates.update(path for path in base.rglob("*") if path.is_file())
    return sorted(path for path in candidates if _allowed(path, root))


def scan(candidate: Path) -> list[dict[str, str]]:
    """Return only rule and relative filename; never return matched secret text."""
    findings: list[dict[str, str]] = []
    for path in sorted(candidate.rglob("*")):
        rel = path.relative_to(candidate).as_posix()
        if path.is_symlink():
            findings.append({"file": rel, "rule": "symbolic link"})
            continue
        if not path.is_file():
            continue
        if set(path.relative_to(candidate).parts) & FORBIDDEN_PARTS:
            findings.append({"file": rel, "rule": "forbidden directory"})
            continue
        if path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name in FORBIDDEN_NAMES:
            findings.append({"file": rel, "rule": "forbidden artifact type"})
            continue
        data = path.read_bytes()
        if b"\x00" in data:
            findings.append({"file": rel, "rule": "unexpected binary content"})
            continue
        for label, pattern in CONTENT_RULES.items():
            matches = list(pattern.finditer(data))
            if label == "literal MAC address":
                matches = [m for m in matches if m.group(0).upper() != b"AA:BB:CC:DD:EE:FF"]
            if matches:
                findings.append({"file": rel, "rule": label})
    return findings


def _tree_inventory(root: Path) -> list[dict[str, object]]:
    return [{
        "path": path.relative_to(root).as_posix(), "size": path.stat().st_size,
        "sha256": _sha(path), "mode": oct(stat.S_IMODE(path.stat().st_mode)),
    } for path in sorted(root.rglob("*")) if path.is_file() and not path.is_symlink()]


def _tree_sha(inventory: list[dict[str, object]]) -> str:
    payload = json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _write_zip(source_dir: Path, archive: Path, root_name: str) -> None:
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for source in sorted(path for path in source_dir.rglob("*") if path.is_file()):
            relative = PurePosixPath(root_name) / source.relative_to(source_dir).as_posix()
            info = zipfile.ZipInfo(str(relative), date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = ((stat.S_IFREG | stat.S_IMODE(source.stat().st_mode)) & 0xFFFF) << 16
            info.create_system = 3
            bundle.writestr(info, source.read_bytes())


def verify_archive(archive: Path, expected_root: str, expected: list[dict[str, object]]) -> dict[str, object]:
    expected_hashes = {str(item["path"]): str(item["sha256"]) for item in expected}
    with tempfile.TemporaryDirectory(prefix="inksight-zip-audit-") as temp_name:
        temp = Path(temp_name)
        seen: set[str] = set()
        with zipfile.ZipFile(archive) as bundle:
            for item in bundle.infolist():
                pure = PurePosixPath(item.filename)
                if pure.is_absolute() or ".." in pure.parts or not pure.parts or pure.parts[0] != expected_root:
                    raise ValueError(f"压缩包包含不安全路径：{item.filename}")
                if item.filename in seen:
                    raise ValueError(f"压缩包包含重复路径：{item.filename}")
                seen.add(item.filename)
                if stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError(f"压缩包包含符号链接：{item.filename}")
            bundle.extractall(temp)
        extracted = temp / expected_root
        actual = _tree_inventory(extracted)
        actual_hashes = {str(item["path"]): str(item["sha256"]) for item in actual}
        if actual_hashes != expected_hashes:
            raise ValueError("压缩包全新解压内容与候选目录不一致。")
        findings = scan(extracted)
        if findings:
            raise ValueError(f"压缩包全新解压隐私扫描发现 {len(findings)} 项问题。")
        return {"entry_count": len(actual_hashes), "bad_paths": 0, "duplicate_paths": 0,
                "symlinks": 0, "nested_archives": 0, "privacy_findings": 0,
                "fresh_extract_matches": True}


def build(root: Path, destination: Path) -> dict[str, object]:
    root, destination = root.resolve(), destination.resolve()
    if destination == root or destination == Path(destination.anchor) or len(destination.parts) < 4:
        raise ValueError("候选目录范围过大，已拒绝。")
    if destination.exists() and not (destination / ".inksight-source-candidate").is_file():
        raise ValueError("目标目录已存在且不是本工具创建的候选目录，已拒绝覆盖。")
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".inksight-build-", dir=destination.parent) as temp_name:
        staging = Path(temp_name) / destination.name
        staging.mkdir()
        (staging / ".inksight-source-candidate").write_text("generated; safe to replace\n", encoding="utf-8")
        copied: list[dict[str, object]] = []
        for source in collect(root):
            rel = source.relative_to(root)
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_source = root / DAILY_MESSAGE_RELEASE_SOURCE if rel == DAILY_MESSAGE_TARGET else source
            shutil.copy2(copy_source, target, follow_symlinks=False)
            copied.append({"path": rel.as_posix(), "size": target.stat().st_size, "sha256": _sha(target)})

        member = staging / "shared/backend/data/member_config.json"
        member.parent.mkdir(parents=True, exist_ok=True)
        member.write_text("{}\n", encoding="utf-8")
        copied.append({"path": member.relative_to(staging).as_posix(), "size": member.stat().st_size, "sha256": _sha(member)})
        copied.sort(key=lambda item: str(item["path"]))
        (staging / "SOURCE-MANIFEST.json").write_text(
            json.dumps({"schema": 2, "allowlisted_source_files": copied}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        (staging / "PUBLICATION-READY.md").write_text(
            "# Engineering release candidate\n\nThis candidate passed the repository's engineering release gates. "
            "Its four bundled daily messages use the publisher-approved GPL-3.0-only release overlay; "
            "the former 12-entry CC0-labelled production corpus is not distributed. "
            "This status is not legal certification. Publication was separately authorized by the repository owner. "
            "Remote content sources remain disabled by default and require separate deployer review before enablement.\n",
            encoding="utf-8")
        findings = scan(staging)
        report = {
            "schema": 2,
            "scope": "allowlisted candidate tree before archive, followed by independent fresh-extract scan",
            "matched_values_included": False, "source_files": len(copied), "findings": findings,
            "coverage": ["filenames and paths", "text content", "forbidden binary/font/archive types",
                         "common API/token/private-key signatures", "absolute home paths", "MAC addresses", "symlinks"],
            "limitations": ["pattern scanning cannot prove absence of every secret",
                            "it cannot establish copyright ownership or external service terms",
                            "encrypted or novel credential formats may not be recognized"],
            "publication_ready": PUBLICATION_READY,
        }
        (staging / "PRIVACY-SCAN.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if findings:
            raise ValueError(f"候选目录隐私扫描发现 {len(findings)} 项问题。")

        inventory = _tree_inventory(staging)
        tree_sha = _tree_sha(inventory)
        archive_tmp = Path(temp_name) / f"{destination.name}.zip"
        _write_zip(staging, archive_tmp, destination.name)
        verification = verify_archive(archive_tmp, destination.name, inventory)
        archive_sha = _sha(archive_tmp)

        if destination.exists():
            shutil.rmtree(destination)
        os.replace(staging, destination)
        archive = destination.with_suffix(".zip")
        os.replace(archive_tmp, archive)
        checksum = archive.with_suffix(archive.suffix + ".sha256")
        checksum.write_text(f"{archive_sha}  {archive.name}\n", encoding="utf-8")
        receipt = archive.with_suffix(archive.suffix + ".receipt.json")
        receipt.write_text(json.dumps({
            "schema": 1, "candidate": destination.name, "source_manifest_count": len(copied),
            "packaged_file_count": len(inventory), "candidate_tree_sha256": tree_sha,
            "archive_sha256": archive_sha, "archive_verification": verification,
            "publication_ready": PUBLICATION_READY,
            "blockers": [],
            "qualifications": [
                "engineering status only; not legal certification",
                "publication was separately authorized by the repository owner",
                "remote content sources require separate deployer review before enablement",
            ],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"files": len(copied), "packaged_files": len(inventory), "findings": [],
            "archive": str(archive), "archive_sha256": archive_sha,
            "candidate_tree_sha256": tree_sha, "verification": verification}


def main() -> int:
    parser = argparse.ArgumentParser(description="构建并独立验证 InkSight 开源候选目录")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    destination = (args.destination or root / "release-candidate" / "InkSight-Source").resolve()
    try:
        report = build(root, destination)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"[阻止] {exc}")
        return 2
    print(f"候选目录：{destination}")
    print(f"允许列表源文件：{report['files']}")
    print(f"压缩包文件：{report['packaged_files']}")
    print("全新解压复核：通过")
    print(f"压缩包 SHA-256：{report['archive_sha256']}")
    print("发布状态：工程候选可发布；这不是法律认证。公开发布已由仓库所有者另行授权。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
