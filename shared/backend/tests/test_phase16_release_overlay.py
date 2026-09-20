from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
APPROVED = ROOT / "docs/phase14-content-source-release-2026-09-14/daily-messages-replacement-draft.json"
RUNTIME = ROOT / "shared/backend/data/daily_messages.json"
PRODUCTION_SHA256 = "4ca19d4fd1f80d32864ee5cd898c7cf8fbd38a2b7a324a877e4467dae94f47f6"
EXPECTED = {
    "draft-one-task": "先选出今天最值得完成的一件事，再为它留出不被打断的一小段时间。结束时记下结果和下一步，明天就能从清楚的位置继续。",
    "draft-check-facts": "把已经确认的事实、仍待核对的信息和自己的推测分开记录。决定会更稳，出现新证据时也更容易调整。",
    "draft-small-loop": "先做一个能够验证的小版本，观察真实反馈，再决定下一处改动。每次只缩小一个不确定范围，进度就会逐渐可靠。",
    "draft-rest": "精力不足时，短暂休息也是任务的一部分。回来后从最轻的一步重新进入，让判断力先恢复，再处理复杂问题。",
}


def _load(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_approved_messages_are_exact_gpl_and_provenance_is_limited():
    data = _payload(APPROVED)
    assert data["status"] == "publisher-approved-for-release-overlay"
    assert data["license"] == "GPL-3.0-only"
    assert {row["id"]: row["text"] for row in data["messages"]} == EXPECTED
    provenance = data["provenance"]
    assert provenance["statement_kind"] == "publisher-statement"
    assert "not independent proof" in provenance["limits"]


def test_source_production_corpus_is_unchanged_but_candidate_runtime_is_overlay():
    data = _payload(RUNTIME)
    if (ROOT / ".inksight-source-candidate").is_file():
        assert data["license"] == "GPL-3.0-only"
        assert {row["id"]: row["text"] for row in data["messages"]} == EXPECTED
    else:
        assert hashlib.sha256(RUNTIME.read_bytes()).hexdigest() == PRODUCTION_SHA256
        assert len(data["messages"]) == 12


def test_four_item_rotation_is_renderable_deterministic_and_bounded(monkeypatch):
    from core import daily_message
    from core import news_validate

    monkeypatch.setattr(daily_message, "DATA_FILE", APPROVED)
    rows = daily_message.load_messages()
    assert len(rows) == 4
    assert all(len(row["lines"]) <= 4 for row in rows)
    assert all(not news_validate.unsupported_chars(row["text"]) for row in rows)

    first = daily_message.choose("2026-09-15", "morning")
    assert first == daily_message.choose("2026-09-15", "morning")
    assert daily_message.choose("2026-09-15", "morning", list(EXPECTED)) in rows

    code = (
        "from pathlib import Path; from core import daily_message; "
        f"daily_message.DATA_FILE=Path({str(APPROVED)!r}); "
        "print(daily_message.choose('2026-09-15','morning')['id'])"
    )
    restarted = subprocess.check_output([sys.executable, "-c", code], cwd=ROOT / "shared/backend", text=True).strip()
    assert restarted == first["id"]


def test_builder_applies_overlay_and_is_deterministic(tmp_path):
    module = _load("shared/tools/release/build_candidate.py", "build_candidate_phase16")
    destination = tmp_path / "InkSight-Source"
    first = module.build(ROOT, destination)
    candidate_data = _payload(destination / "shared/backend/data/daily_messages.json")
    assert candidate_data["license"] == "GPL-3.0-only"
    assert {row["id"]: row["text"] for row in candidate_data["messages"]} == EXPECTED
    assert "CC0-1.0" not in (destination / "shared/backend/data/daily_messages.json").read_text(encoding="utf-8")
    assert (destination / "PUBLICATION-READY.md").is_file()
    assert not (destination / "PUBLICATION-BLOCKED.md").exists()

    receipt = _payload(destination.with_suffix(".zip.receipt.json"))
    assert receipt["publication_ready"] is True and receipt["blockers"] == []
    with zipfile.ZipFile(destination.with_suffix(".zip")) as bundle:
        packed = json.loads(bundle.read("InkSight-Source/shared/backend/data/daily_messages.json"))
    assert packed == candidate_data

    second = module.build(ROOT, destination)
    assert second["archive_sha256"] == first["archive_sha256"]
    assert second["candidate_tree_sha256"] == first["candidate_tree_sha256"]


def test_current_license_documents_agree_on_engineering_status():
    scope = (ROOT / "LICENSE-SCOPE.md").read_text(encoding="utf-8")
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    sbom = _payload(ROOT / "SBOM.json")
    assert "GPL-3.0-only" in scope and "not legal" in scope
    assert "publisher" in notices.lower() and "statement" in notices.lower()
    asset = next(item for item in sbom["content_assets"] if item["name"] == "release daily messages")
    assert asset["license"] == "GPL-3.0-only" and asset["entries"] == 4
    assert asset["old_cc0_corpus_distributed"] is False
    generator = (ROOT / "shared/tools/release/generate_release_metadata.py").read_text(encoding="utf-8")
    assert "Owner confirmation still required" not in generator
    assert '"license": "GPL-3.0-only"' in generator
