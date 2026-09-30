from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "shared/tools/app_data.py"
SPEC = importlib.util.spec_from_file_location("app_data_tested", SCRIPT)
assert SPEC and SPEC.loader
app_data = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app_data)


def template_at(tmp_path: Path) -> Path:
    template = tmp_path / "template"
    (template / "shared/backend").mkdir(parents=True)
    (template / "shared/config").mkdir(parents=True)
    (template / ".inksight-source-candidate").write_text("verified\n")
    (template / "shared/backend/.env.example").write_text(
        "ADMIN_TOKEN=\nDEEPSEEK_API_KEY=\n", encoding="utf-8")
    (template / "shared/config/inksight_config.example.json").write_text('{"schema_version": 2}')
    (template / "shared/config/inksight_secrets.example.json").write_text('{"schema_version": 2}')
    return template


def test_new_install_is_private_repeatable_and_has_no_default_token(tmp_path):
    template = template_at(tmp_path)
    data = tmp_path / "用户 数据"
    first = app_data.ensure_install(template, data, "v0.1.0-test.3")
    runtime = Path(first["runtime"])
    env = runtime / "shared/backend/.env"
    assert first["status"] == "installed"
    assert "ADMIN_TOKEN=" in env.read_text()
    assert "ADMIN_TOKEN=\n" not in env.read_text()
    assert os.stat(env).st_mode & 0o077 == 0
    assert os.stat(data).st_mode & 0o077 == 0
    assert (runtime / "shared/config/inksight_config.json").is_file()
    assert os.stat(runtime / "shared/config/inksight_secrets.json").st_mode & 0o077 == 0
    assert app_data.ensure_install(template, data, "v0.1.0-test.3")["status"] == "already-installed"
    assert Path(json.loads((data / "current.json").read_text())["version"]) == Path("v0.1.0-test.3")


def test_version_replacement_preserves_config_state_and_old_runtime(tmp_path):
    template = template_at(tmp_path)
    data = tmp_path / "data"
    old = Path(app_data.ensure_install(template, data, "v0.1.0-test.3")["runtime"])
    secret = old / "shared/config/inksight_secrets.json"
    secret.write_text('{"private": "kept"}')
    state = old / "shared/backend/state/gold.json"
    state.parent.mkdir()
    state.write_text('{"baseline": 12}')
    db = old / "shared/backend/inksight.db"
    db.write_bytes(b"database")
    latest = Path(app_data.ensure_install(template, data, "v0.1.0-test.4")["runtime"])
    assert latest != old
    assert secret.read_bytes() == (latest / "shared/config/inksight_secrets.json").read_bytes()
    assert state.read_bytes() == (latest / "shared/backend/state/gold.json").read_bytes()
    assert db.read_bytes() == (latest / "shared/backend/inksight.db").read_bytes()
    assert old.is_dir()  # rollback generation remains intact


def test_explicit_import_dry_run_then_backup(tmp_path):
    template = template_at(tmp_path)
    data = tmp_path / "data"
    runtime = Path(app_data.ensure_install(template, data, "v0.1.0-test.3")["runtime"])
    source = tmp_path / "old source"
    (source / "shared/config").mkdir(parents=True)
    (source / "shared/backend").mkdir(parents=True)
    (source / "shared/config/inksight_secrets.json").write_text('{"private": 1}')
    preview = app_data.import_existing(source, data)
    assert preview["status"] == "dry-run"
    assert (runtime / "shared/config/inksight_secrets.json").read_text() == '{"schema_version": 2}'
    result = app_data.import_existing(source, data, confirm=True)
    assert result["status"] == "imported"
    assert (runtime / "shared/config/inksight_secrets.json").read_text() == '{"private": 1}'
    assert Path(result["backup"]).is_dir()


def test_import_rejects_symlink_and_leaves_runtime_unchanged(tmp_path):
    template = template_at(tmp_path)
    data = tmp_path / "data"
    runtime = Path(app_data.ensure_install(template, data, "v0.1.0-test.3")["runtime"])
    source = tmp_path / "old"
    (source / "shared/config").mkdir(parents=True)
    (source / "shared/backend/state").mkdir(parents=True)
    (source / "shared/backend/state/gold.json").symlink_to(template / ".inksight-source-candidate")
    with pytest.raises(ValueError, match="symbolic link"):
        app_data.import_existing(source, data, confirm=True)
    assert runtime.is_dir()
    assert not (runtime / "shared/backend/state/gold.json").exists()


def test_upgrade_rejects_runtime_symlink_before_following_it(tmp_path):
    template = template_at(tmp_path)
    data = tmp_path / "data"
    runtime = Path(app_data.ensure_install(template, data, "v0.1.0-test.3")["runtime"])
    (runtime / "external").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symbolic link"):
        app_data.ensure_install(template, data, "v0.1.0-test.4")
    assert json.loads((data / "current.json").read_text())["version"] == "v0.1.0-test.3"


def test_import_preserves_user_calendars_messages_policy_and_private_fonts(tmp_path):
    template = template_at(tmp_path)
    data = tmp_path / "data"
    runtime = Path(app_data.ensure_install(template, data, "v0.1.0-test.5")["runtime"])
    source = tmp_path / "old"
    (source / "shared/config").mkdir(parents=True)
    fixtures = {
        "shared/backend/data/news_calendar.json": b'{"custom-calendar":true}',
        "shared/backend/data/daily_messages.json": b'["personal message"]',
        "shared/tools/agent_policy.json": b'{"publish_sec":120}',
        ".local/fonts/misans/private-font.ttf": b"private-font-fixture",
        "shared/backend/fonts/misans/private-font.ttf": b"private-preview-font-fixture",
        "shared/firmware/src/fonts_gen/misans_reg_21.h": b"private-device-width-fixture",
    }
    for relative, content in fixtures.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    app_data.import_existing(source, data, confirm=True)
    for relative, content in fixtures.items():
        assert (runtime / relative).read_bytes() == content
        assert (runtime / relative).stat().st_mode & 0o077 == 0
