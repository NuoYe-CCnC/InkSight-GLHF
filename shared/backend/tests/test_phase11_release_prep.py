from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_example_is_exact_default_and_valid():
    from core import operator_config

    example = json.loads((ROOT / "shared/config/inksight_config.example.json").read_text(encoding="utf-8"))
    assert example == operator_config.DEFAULT_CONFIG
    assert operator_config.validate_config(example) == []


def test_manager_exposes_real_policy_controls():
    html = (ROOT / "shared/backend/static/manager/index.html").read_text(encoding="utf-8")
    script = (ROOT / "shared/backend/static/manager/manager.js").read_text(encoding="utf-8")
    for field in (
        "newsCheckEnabled", "newsFollowup", "newsFollowupPoll", "observationSeconds",
        "observationPoll", "unknownMaxAge", "adaptiveActive", "adaptiveLight",
        "adaptiveNight", "manualWorkdays", "manualHolidays",
    ):
        assert f'id="{field}"' in html
        assert f'$("{field}")' in script
    assert "collectPolicySettings(collectSettings())" in script


def test_font_import_rejects_non_font_without_copying(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "import_misans", ROOT / "shared/tools/import_misans.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    fake = tmp_path / "MiSans-Regular.otf"
    fake.write_bytes(b"not-a-font")
    try:
        module.validate_font(fake, "常规", "中文")
    except module.FontImportError as exc:
        assert "文件头无效" in str(exc)
    else:
        raise AssertionError("invalid font was accepted")
    assert not (tmp_path / ".local").exists()
