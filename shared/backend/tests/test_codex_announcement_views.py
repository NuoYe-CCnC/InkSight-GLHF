"""Synthetic bitmap, optional snapshot and cross-language expiry contracts."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest
from core import activity_signals, codex_announcement_view as view

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("forecast_preview", ROOT / "shared/tools/misans_panel_render.py")
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)
NOW = 1792728000  # Synthetic 2026-10-23 12:00 Beijing, never live-account data.


def forecasts():
    def event(kind, target):
        return dict(kind=kind, status="pending", confidence="explicit", target_at=target,
                    expires_at=target, valid_until=target, published_at=NOW-60, post_id="1234567890000000001")
    return dict(schema_version=1, enabled=True, quota_reset=event("quota_reset", NOW+3600),
                reset_grant=event("reset_grant", NOW+7200))


def document():
    return {"ts": NOW, "screen": {"display_preferences": {}, "pages": {"ai": {
        "plan": "PLUS", "valid_until_date": "2026-11-23", "codex_7d_used": 37,
        "codex_resets_at": NOW+86400, "reset_credits_available": 2,
        "reset_credits_confirmed": True, "reset_expiry_list": [NOW+86400*3],
        "deepseek": {"CNY": 11.05, "USD": 0}, "deepseek_today_tokens": 5967,
        "deepseek_today_tokens_complete": True, "codex_credit_balance": "12.5",
        "openai_api_month_spend_usd": 20}, "news_gold": {}}}}


@pytest.fixture(scope="module")
def fonts():
    directory = os.environ.get("INKSIGHT_TEST_FONT_DIR")
    if not directory: pytest.skip("private non-distributed fonts required")
    preview.FONT_DIR = Path(directory)
    return preview.load_fonts()


def paint(doc, fonts, now=NOW, news=False):
    panel = preview.Panel()
    (preview.render_news_gold_panel if news else preview.render_ai_panel)(panel, fonts, doc, now)
    return panel


@pytest.mark.parametrize("mutation", [
    lambda s: s.update(enabled=False), lambda s: s.update(schema_version=True),
    lambda s: s.update(schema_version=2),
    lambda s: s["quota_reset"].update(target_at=True),
    lambda s: s["quota_reset"].update(valid_until=NOW),
    lambda s: s["quota_reset"].update(published_at=NOW+1),
    lambda s: s["quota_reset"].update(expires_at=NOW+3601),
    lambda s: s["quota_reset"].update(confidence="guess"),
])
def test_fail_closed_snapshot_renderer_parity(mutation):
    snap=forecasts(); snap["reset_grant"]=None; mutation(snap)
    assert view.visible_events(snap, NOW) == {}
    assert preview.announcement_target({"announcements":snap}, "quota_reset", NOW) == 0


@pytest.mark.parametrize("credits", [0, 2, None, 999, 2147483647])
def test_actual_misans_forecast_fit_and_baselines(fonts, credits):
    doc=document(); ai=doc["screen"]["pages"]["ai"]
    ai.update(announcements=forecasts(), reset_credits_available=credits)
    panel=paint(doc,fonts)
    assert not panel.missing, [hex(c) for c in panel.missing]
    upper=[op for op in panel.ops if op["text"]=="预计" or "前重置" in op["text"]]
    assert [op["baseline"] for op in upper]==[225,256]
    assert all(op["ink"][1]==461 for op in upper)
    lower=[op for op in panel.ops if '预计' in op['text'] and op['baseline']>=343]
    assert not lower
    assert not any(op["text"]=="API 本月消费" and op['baseline']<343 for op in panel.ops)
    if credits != 0:
        assert any(op["baseline"]==407 and op["text"].startswith("到期") for op in panel.ops)
    else:
        assert any(op['baseline']==377 for op in panel.ops)
    # No upper/lower cross-column ink collision and no progress-bar movement.
    for op in upper:
        left=next(x for x in panel.ops if x["text"]==("剩余" if op["baseline"]==225 else "7 日窗口"))
        assert left["ink"][1]+16 <= op["ink"][0]


@pytest.mark.parametrize("show_codex,show_api", [(False,False),(True,False),(False,True),(True,True)])
def test_expired_restores_byte_identical_old_layout_and_news_unchanged(fonts,show_codex,show_api):
    doc=document(); doc["screen"]["display_preferences"].update(show_codex_credits=show_codex,show_openai_api_info=show_api)
    old=paint(doc,fonts,now=NOW+7200); news=paint(doc,fonts,news=True)
    doc["screen"]["pages"]["ai"]["announcements"]=forecasts()
    assert paint(doc,fonts,now=NOW+7200).fb==old.fb
    assert paint(doc,fonts,news=True).fb==news.fb
    active=paint(doc,fonts)
    assert not any(op["text"] in {"API 本月消费","点数余额"} for op in active.ops)
    assert doc["screen"]["display_preferences"]["show_codex_credits"]==show_codex


def test_visual_only_not_activity_or_news_keys():
    ai=document()["screen"]["pages"]["ai"]
    original=copy.deepcopy(ai)
    ai["announcements"]=forecasts()
    assert activity_signals.ai_visual_key(ai,now=NOW)!=activity_signals.ai_visual_key(original,now=NOW)
    assert activity_signals.ai_visual_key(ai,now=NOW+7200)==activity_signals.ai_visual_key(original,now=NOW+7200)
    assert activity_signals.activity_snapshot(ai)==activity_signals.activity_snapshot(original)
    assert activity_signals.activity_key(ai)==activity_signals.activity_key(original)
    assert activity_signals.news_gold_visual_key(ai,{})==activity_signals.news_gold_visual_key(original,{})
    key=activity_signals.ai_visual_key(ai,now=NOW)
    ai["announcements"]["last_polled_at"]=NOW+1
    assert activity_signals.ai_visual_key(ai,now=NOW+1)==key


def test_explicit_hide_manual_reset_still_honoured(fonts):
    doc=document(); doc["screen"]["display_preferences"]["show_reset_opportunities"]=False
    doc["screen"]["pages"]["ai"]["announcements"]=forecasts()
    assert not any("发放" in op["text"] or "次可用" in op["text"] for op in paint(doc,fonts).ops)


def test_firmware_optional_snapshot_host_contract(tmp_path):
    includes=os.environ.get("INKSIGHT_TEST_ARDUINOJSON_INCLUDE")
    if not includes: pytest.skip("ArduinoJson headers required")
    native=tmp_path/"forecast-host"
    subprocess.run(["c++","-std=c++17","-I"+includes,"-I"+str(ROOT/"shared/firmware/src"),
                    str(ROOT/"shared/backend/tests/codex_announcement_host.cpp"),"-o",str(native)],check=True,capture_output=True)
    snap=forecasts()
    for clock in [NOW,NOW+3600,NOW+7200]:
        expected={k:preview.announcement_target({"announcements":snap},k,clock) for k in ("quota_reset","reset_grant")}
        data={"now":clock,"ai":{"announcements":snap}}
        got=json.loads(subprocess.run([str(native)],input=json.dumps(data),text=True,capture_output=True,check=True).stdout)
        assert got==expected
