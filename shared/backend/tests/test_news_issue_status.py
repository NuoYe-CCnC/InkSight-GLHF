"""Synthetic, network-free issue/renderer/firmware policy regressions."""
from __future__ import annotations
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import pytest
from core import news_brief, news_calendar, news_schedule, activity_signals

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("issue_preview", ROOT / "shared/tools/misans_panel_render.py")
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)
BJ = dt.timezone(dt.timedelta(hours=8))


def clock(value):
    return dt.datetime.fromisoformat(value)


def epoch(value):
    return int(clock(value).replace(tzinfo=BJ).timestamp())


@pytest.fixture
def policy(monkeypatch):
    cfg = {"enabled": True, "workday_cutoff": "22:00", "restday_cutoff": "20:00",
           "allow_missed_catchup": True, "max_calls_per_issue": 5,
           "schedules": [{"id": ident, "label": "科技 / AI "+label,
                          "time": at, "enabled": True, "day_types": kinds}
                         for ident, label, at, kinds in [
                             ("morning", "早报", "08:55", ["all"]),
                             ("noon", "中报", "12:55", ["workday"]),
                             ("evening", "晚报", "16:55", ["workday"])]]}
    state = {"done": {}, "inflight": {}, "model_calls": {}}
    source = {"current": None}
    monkeypatch.setattr(news_schedule, "_config", lambda: copy.deepcopy(cfg))
    monkeypatch.setattr(news_schedule, "_load", lambda: copy.deepcopy(state))
    monkeypatch.setattr(news_brief, "_load_state", lambda: copy.deepcopy(source))
    monkeypatch.setattr(news_schedule, "credential_state", lambda: {"state": "active"})
    monkeypatch.setattr(news_calendar, "is_workday", lambda now: now.weekday() < 5 and now.strftime("%Y-%m-%d") != "2026-10-01")
    monkeypatch.setattr(news_schedule.digest, "validate_digest_text", lambda text: {"ok": True, "lines": [text]})
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("network forbidden"))
    return cfg, state, source


def body(ident="noon", date="2026-09-30", mode="digest"):
    return {"mode": mode, "date": date, "period": ident, "schedule_id": ident,
            "issue_title": "合成期刊", "generated_at": epoch(date+"T12:55:36"),
            "text": "合成测试正文，不是实屏新闻。", "freshness": "fresh", "origin": "local-fallback"}


def document(news, cfg, now):
    date = time.strftime("%Y-%m-%d", time.gmtime(now+28800))
    ai = {"codex_7d_used": 3, "plan": "PLUS", "valid_until_date": "2026-10-28",
          "deepseek": {"CNY": 1.23, "USD": 0},
          "fresh": {"codex": now, "ds_cny": now, "ds_usd": now}}
    return {"ts": now, "_test_now": now, "screen": {
        "calendar": {"days": [{"d": date, "workday": date != "2026-10-01"}]},
        "device_policy": {"news_check": {"schedules": [
            {**row, "display_label": news_schedule.issue_display_label(row["label"])} for row in cfg["schedules"]]}},
        "pages": {"ai": ai, "news_gold": {"news": news,
                    "gold": {"price_gram_cny": 1, "spot_usd_oz": 1, "fx_rate": 1, "price_as_of": now}}}}}


def snap(policy, value, ident="noon", **kwargs):
    cfg, state, source = policy
    source["current"] = body(ident, **kwargs)
    return news_schedule.news_publication_snapshot(clock(value))


@pytest.mark.parametrize("at,kind,expected", [
    ("16:54:59", "waiting", "数据正常"),
    ("16:55:00", "waiting", "晚报待更新"),
    ("16:55:10", "generating", "晚报生成中"),
    ("16:56:00", "failed", "晚报暂未更新"),
    ("16:56:00", "published", "数据正常"),
    ("22:01:00", "failed", "晚报暂未更新"),
])
def test_evening_boundaries(policy, at, kind, expected):
    cfg, state, source = policy
    value = "2026-09-30T"+at
    if kind == "generating":
        state["inflight"]["2026-09-30|evening"] = {"started_at": epoch(value)-1}
    if kind == "failed":
        state["done"]["2026-09-30|evening"] = "skip:max-model-calls"
    news = snap(policy, value, "evening" if kind == "published" else "noon")
    doc = document(news, cfg, epoch(value))
    assert preview.page_status_text(doc, "news_gold", epoch(value), label_validator=lambda label: True) == expected


def test_published_body_before_ledger_is_not_generating(policy):
    cfg, state, source = policy
    state["inflight"]["2026-09-30|evening"] = {"started_at": epoch("2026-09-30T16:55:00")}
    news = snap(policy, "2026-09-30T16:55:16", "evening")
    assert news["issue_status"]["state"] == "published"
    assert news["origin"] == "local-fallback"


def test_morning_failure_noon_success_and_fallback(policy):
    cfg, state, source = policy
    state["done"]["2026-09-30|morning"] = "skip:max-model-calls"
    news = snap(policy, "2026-09-30T15:00:00", mode="daily_message")
    assert news["issue_status"]["state"] == "published"
    assert news["issue_status"]["expected"]["id"] == "noon"


@pytest.mark.parametrize("day,expected", [("2026-09-30", "昨日内容"), ("2026-09-28", "资讯陈旧")])
def test_cross_day_first_issue(policy, day, expected):
    cfg, state, source = policy
    news = snap(policy, "2026-10-01T00:01:00", date=day)
    assert news["freshness"] == "stale"
    doc = document(news, cfg, epoch("2026-10-01T00:01:00"))
    assert preview.page_status_text(doc, "news_gold", doc["_test_now"]) == expected


def test_restday_and_custom_reverse_schedule(policy):
    cfg, state, source = policy
    news = snap(policy, "2026-10-01T17:00:00", "morning", date="2026-10-01")
    assert news["issue_status"]["expected"]["display_label"] == "日报"
    cfg["schedules"][2].update(id="custom-pm", label="研究简报", time="17:30")
    cfg["schedules"].reverse()
    news = snap(policy, "2026-09-30T17:00:00")
    assert news["issue_status"]["state"] == "published"
    news = snap(policy, "2026-09-30T17:30:00")
    doc = document(news, cfg, epoch("2026-09-30T17:30:00"))
    assert preview.page_status_text(doc, "news_gold", doc["_test_now"], label_validator=lambda label: True) == "研究简报待更新"


@pytest.mark.parametrize("custom,expected", [(False, "日报待更新"), (True, "假日观察待更新")])
def test_restday_waiting_uses_snapshot_label(policy, custom, expected):
    cfg, _, source = policy
    if custom:
        cfg["schedules"][0]["label"] = "假日观察"
    source["current"] = body(date="2026-09-30")
    news = news_schedule.news_publication_snapshot(clock("2026-10-01T08:55:00"))
    doc = document(news, cfg, epoch("2026-10-01T08:55:00"))
    assert preview.news_issue_status_text(doc, doc["_test_now"], lambda label: True) == expected


@pytest.mark.parametrize("at,state,expected", [
    ("08:54:59", "not-due-empty", "资讯待更新"),
    ("08:55:00", "due", "早报待更新"),
    ("16:55:00", "due", "晚报待更新"),
    ("22:00:00", "expired", "晚报暂未更新"),
])
def test_cold_start_empty_body_contract(policy, at, state, expected):
    cfg, _, _ = policy
    value = "2026-09-30T"+at
    news = news_schedule.news_publication_snapshot(clock(value))
    assert news["mode"] == "status" and news["text"] == ""
    assert news["issue_status"]["state"] == state
    assert news["issue_status"]["current"]["date"] is None
    assert news["freshness"] == "missing"
    assert not any(key in news for key in ("china", "world", "tech"))
    doc = document(news, cfg, epoch(value))
    assert preview.news_issue_status_text(doc, doc["_test_now"], lambda label: True) == expected


def test_corrupt_schedule_ledger_is_unknown(policy):
    cfg, state, _ = policy
    state["_state_error"] = "corrupt:test"
    news = snap(policy, "2026-09-30T16:55:00")
    assert news["issue_status"]["state"] == "unknown"
    doc = document(news, cfg, epoch("2026-09-30T16:55:00"))
    assert preview.news_issue_status_text(doc, doc["_test_now"], lambda label: True) == "资讯待更新"


def test_degraded_calendar_does_not_claim_an_issue_name(policy, monkeypatch):
    monkeypatch.setattr(news_calendar, "workday_info", lambda now: {"degraded": True})
    news = snap(policy, "2026-09-30T16:55:00")
    assert news["issue_status"]["state"] == "unknown"
    assert news["issue_status"]["calendar_trusted"] is False


def test_matching_news_preserves_other_data_quality(policy):
    cfg, _, _ = policy
    news = snap(policy, "2026-09-30T16:56:00", "evening")
    doc = document(news, cfg, epoch("2026-09-30T16:56:00"))
    doc["screen"]["pages"]["news_gold"]["gold"]["stale"] = True
    assert preview.page_status_text(doc, "news_gold", doc["_test_now"]) == "部分数据陈旧"


def test_timezone_independent_and_clock_unknown(policy, monkeypatch):
    a = snap(policy, "2026-09-30T16:55:00")
    b = news_schedule.news_publication_snapshot(dt.datetime(2026,9,30,8,55,tzinfo=dt.timezone.utc))
    assert a == b
    assert a["issue_status"]["expected"]["planned_at"] == epoch("2026-09-30T16:55:00")
    cfg, state, source = policy
    doc = document(a, cfg, epoch("2026-09-30T16:55:00"))
    assert preview.page_status_text(doc, "news_gold", 100) == "时间待校准"
    doc["ts"] += 600
    assert preview.news_issue_status_text(doc, doc["_test_now"]) == "资讯待更新"


def test_snapshot_retries_concurrent_replacement(policy, monkeypatch):
    cfg, state, source = policy
    source["current"] = body()
    calls = [0]
    def read():
        calls[0] += 1
        return {"current": body("noon" if calls[0] == 1 else "evening")}
    monkeypatch.setattr(news_brief, "_load_state", read)
    news = news_schedule.news_publication_snapshot(clock("2026-09-30T17:00:00"))
    assert calls[0] == 4
    assert news["schedule_id"] == news["issue_status"]["current"]["id"] == "evening"
    assert news["issue_status"]["coherent"]


def test_unstable_snapshot_degrades_not_guesses(policy, monkeypatch):
    calls = [0]
    def read():
        calls[0] += 1
        return {"current": {**body(), "version": str(calls[0])}}
    monkeypatch.setattr(news_brief, "_load_state", read)
    news = news_schedule.news_publication_snapshot(clock("2026-09-30T15:00:00"))
    assert not news["issue_status"]["coherent"]
    cfg, _, _ = policy
    doc = document(news, cfg, epoch("2026-09-30T15:00:00"))
    assert preview.page_status_text(doc,"news_gold",doc["_test_now"]) == "资讯待更新"


def test_visual_keys_track_status_not_heartbeat(policy):
    cfg, _, _ = policy
    a = snap(policy,"2026-09-30T16:55:01")
    b = snap(policy,"2026-09-30T16:55:31")
    assert activity_signals.news_gold_visual_key({}, {"news": a}) == activity_signals.news_gold_visual_key({}, {"news": b})
    b["issue_status"]["state"] = "generating"
    assert activity_signals.news_gold_visual_key({}, {"news": a}) != activity_signals.news_gold_visual_key({}, {"news": b})


@pytest.mark.parametrize("label", ["<bad>", "含\n换行", "过长名称"*4, "emoji📰"])
def test_unsafe_labels_generic(label):
    assert news_schedule.issue_display_label(label) == "资讯"


@pytest.fixture(scope="module")
def native(tmp_path_factory):
    include = os.environ.get("INKSIGHT_TEST_ARDUINOJSON_INCLUDE")
    if not include:
        pytest.skip("set INKSIGHT_TEST_ARDUINOJSON_INCLUDE for offline native policy verification")
    output = tmp_path_factory.mktemp("news-native")/"policy"
    subprocess.run(["clang++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                    "-I"+include, "-I"+str(ROOT/"shared/firmware/src"),
                    str(Path(__file__).with_name("news_issue_status_host.cpp")), "-o", str(output)], check=True)
    return output


@pytest.mark.parametrize("variant", ["waiting", "generating", "failed", "published", "local",
    "obsolete", "expired-local", "wrong-date", "old-backend", "unknown-clock", "bad-calendar",
    "yesterday", "older", "changed-expected", "bad-body-metadata", "unsafe-label", "missing-glyph", "offline-cache",
    "restday", "cold-start", "cold-start-before", "unreachable", "future-payload", "incoherent", "old-local", "degraded-calendar"])
def test_native_and_mirror(policy, native, variant):
    cfg, state, source = policy
    value = "2026-09-30T16:55:10"
    if variant == "generating":
        state["inflight"]["2026-09-30|evening"] = {"started_at": epoch(value)-1}
    if variant == "failed": state["done"]["2026-09-30|evening"] = "skip:max-model-calls"
    news = snap(policy,value,"evening" if variant in {"published","obsolete"} else "noon")
    doc = document(news,cfg,epoch(value))
    if variant in {"restday", "cold-start", "cold-start-before"}:
        value = "2026-10-01T08:55:00" if variant == "restday" else (
            "2026-09-30T08:54:59" if variant == "cold-start-before" else "2026-09-30T16:55:00")
        source["current"] = body() if variant == "restday" else None
        news = news_schedule.news_publication_snapshot(clock(value))
        doc = document(news, cfg, epoch(value))
    if variant == "unreachable":
        news.update(device_update_state="unreachable", device_update_issue_key="2026-09-30|evening",
                    device_update_expires_at=doc["_test_now"]+300)
    if variant == "future-payload": doc["ts"] += 600
    if variant == "incoherent": news["issue_status"]["coherent"] = False
    if variant == "old-local": news.update(device_update_state="requested",device_update_expires_at=doc["_test_now"]+300)
    if variant in {"local","obsolete","expired-local","wrong-date"}:
        news.update(device_update_state="requested",device_update_issue_key="2026-09-30|evening",device_update_expires_at=doc["_test_now"]+300)
        if variant == "expired-local": news["device_update_expires_at"] = doc["_test_now"]-1
        if variant == "wrong-date": news["device_update_issue_key"] = "2026-09-29|evening"
    if variant == "old-backend": news.pop("issue_status")
    if variant == "unknown-clock": doc["_test_now"] = 100
    if variant == "bad-calendar": doc["screen"]["calendar"]["days"] = []
    if variant == "degraded-calendar": doc["screen"]["calendar"]["days"][0]["workday_degraded"] = True
    if variant in {"yesterday","older"}:
        news["date"] = "2026-09-30" if variant == "yesterday" else "2026-09-28"
        doc["_test_now"] = doc["ts"] = epoch("2026-10-01T00:01:00")
        doc["screen"]["calendar"]["days"] = [{"d":"2026-10-01","workday":False}]
    if variant == "changed-expected":
        cfg["schedules"][2]["id"] = "changed"
        doc["screen"]["device_policy"]["news_check"]["schedules"][2]["id"] = "changed"
    if variant == "bad-body-metadata": news["issue_status"]["current"]["id"] = "morning"
    if variant in {"unsafe-label","missing-glyph"}:
        news["issue_status"]["expected"]["display_label"] = "<bad>" if variant == "unsafe-label" else "侃"
    if variant == "offline-cache": doc["ts"] -= 1200;news["issue_status"]["state"]="generating"
    safe = lambda label: bool(label and len(label.encode())<=36 and "\n" not in label and "侃" not in label and "<" not in label)
    expected = preview.news_issue_status_text(doc,doc["_test_now"],safe) or ""
    result = json.loads(subprocess.run([str(native)],input=json.dumps(doc)+"\n",capture_output=True,text=True,check=True).stdout)
    assert result["status"] == expected
    required = {"restday": "日报待更新", "cold-start": "晚报待更新",
                "cold-start-before": "资讯待更新", "unsafe-label": "资讯待更新",
                "missing-glyph": "资讯待更新", "unreachable": "晚报暂未更新"}
    if variant in required: assert result["status"] == required[variant]
    if variant in {"obsolete","expired-local","wrong-date"}: assert result["local"] == ""
    if variant == "local": assert result["local"] == "requested"


def test_actual_misans_status_glyphs_and_width():
    directory = os.environ.get("INKSIGHT_TEST_FONT_DIR")
    if not directory: pytest.skip("set INKSIGHT_TEST_FONT_DIR to verify private, non-distributed MiSans fonts")
    preview.FONT_DIR = Path(directory)
    fonts = preview.load_fonts()
    painter = preview.Panel()
    for text in ["晚报待更新","晚报生成中","晚报暂未更新","日报待更新","日报生成中","日报暂未更新",
                 "昨日内容","资讯陈旧","资讯待更新","资讯暂未更新", "资讯生成中"]:
        assert all(fonts["reg21"].glyph_index(ord(c)) >= 0 for c in text), text
        line = preview.fmt_upd_text(text,epoch("2026-09-30T16:55:10"),epoch("2026-09-30T16:55:10"))
        assert painter.ink_width(fonts["reg21"],line) <= 754-painter.ink_width(fonts["reg21"],"INKSIGHT")-16


@pytest.fixture(scope="module")
def real_fonts():
    directory = os.environ.get("INKSIGHT_TEST_FONT_DIR")
    if not directory:
        pytest.skip("local font assets required, never distributed")
    preview.FONT_DIR = Path(directory)
    return preview.load_fonts()


@pytest.mark.parametrize("state,label", [
    ("due", "晚报"), ("generating", "晚报"), ("failed", "晚报"),
    ("due", "日报"), ("due", "W"*12), ("due", "晚"*13), ("due", "侃"),
])
def test_actual_footer_render_is_bounded(policy, real_fonts, state, label):
    cfg, _, _ = policy
    value = "2026-09-30T16:55:10"
    news = snap(policy, value)
    news["issue_status"]["state"] = state
    news["issue_status"]["expected"]["display_label"] = label
    news["issue_title"] = "科技 / AI 中报"
    news["text"] = "本页数据为合成测试，不是实时新闻。"
    doc = document(news, cfg, epoch(value))
    doc["screen"]["pages"]["ai"].update(deepseek_today_tokens=5967, deepseek_today_tokens_complete=True)
    painter = preview.Panel()
    preview.render_news_gold_panel(painter, real_fonts, doc, doc["_test_now"])
    foot = [op for op in painter.ops if "更新" in op["text"]]
    brand = [op for op in painter.ops if op["text"] == "INKSIGHT"]
    assert len(foot) == len(brand) == 1
    assert foot[0]["baseline"] == brand[0]["baseline"] == 461
    assert foot[0]["ink"][1]+16 <= brand[0]["ink"][0]
    assert all(real_fonts["reg21"].glyph_index(ord(c)) >= 0 for c in foot[0]["text"])
    if label in {"晚"*13, "侃"}:
        assert foot[0]["text"].startswith("资讯待更新")
    destination = os.environ.get("INKSIGHT_TEST_RENDER_DIR")
    if destination:
        # Generated synthetic evidence only, outside source and release assets.
        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        name = state+"-"+("ascii-long" if label.startswith("W") else "oversize" if len(label) > 12 else label)
        preview.raw_to_png(bytes(painter.fb), str(directory/(name+"-synthetic.png")))
        (directory/(name+"-layout.json")).write_text(json.dumps({
            "synthetic": True, "physical_screen_verified": False,
            "status": foot[0], "brand": brand[0],
            "missing_glyphs": [hex(c) for c in painter.missing],
        }, ensure_ascii=False, indent=2))


def test_news_status_cannot_change_ai_page_or_body_layout(policy, real_fonts):
    cfg, _, _ = policy
    value = "2026-09-30T16:55:10"
    news = snap(policy, value)
    news["issue_title"] = "科技 / AI 中报"
    news["text"] = "本页数据为合成测试，不是实时新闻。"
    doc = document(news, cfg, epoch(value))
    ai = preview.Panel()
    before = preview.Panel()
    preview.render_ai_panel(ai, real_fonts, doc, doc["_test_now"])
    preview.render_news_gold_panel(before, real_fonts, doc, doc["_test_now"])
    news["issue_status"]["state"] = "failed"
    ai_after = preview.Panel()
    after = preview.Panel()
    preview.render_ai_panel(ai_after, real_fonts, doc, doc["_test_now"])
    preview.render_news_gold_panel(after, real_fonts, doc, doc["_test_now"])
    assert ai.fb == ai_after.fb
    assert before.fb[:440*preview.ROW_BYTES] == after.fb[:440*preview.ROW_BYTES]


def test_structured_cloud_transport_preserves_same_issue_snapshot(policy, monkeypatch):
    from types import SimpleNamespace
    from core import structured_payload, operator_config, date_table
    sys_path = str(ROOT/"shared/tools")
    if sys_path not in sys.path: sys.path.insert(0, sys_path)
    import cloud_publish
    cfg, _, _ = policy
    news = snap(policy, "2026-09-30T16:55:00")
    monkeypatch.setattr(operator_config, "load_effective", lambda: SimpleNamespace(config={
        "news_digest": copy.deepcopy(cfg), "device_policy": {}, "panel_display": {}}))
    monkeypatch.setattr(date_table, "build_table_payload", lambda **kw: {"days": [{"d":"2026-09-30","workday":True}]})
    data = {"feed_news": news, "feed_ai": {}, "feed_gold": {},
            "feed_versions": {"news": "synthetic-body-version"}}
    result = structured_payload.build_screen_payload("AI_USAGE", data, 800, 480)
    assert result["screen"]["pages"]["news_gold"]["news"] == news
    combined = cloud_publish.combine_dashboard([result])
    assert combined["screen"]["pages"]["news_gold"]["news"] == news
    assert combined["screen"]["versions"]["news_gold_visual_key"] == result["screen"]["versions"]["news_gold_visual_key"]


@pytest.mark.parametrize("unavailable", [False, True])
def test_ai_content_attach_has_no_second_body_read_or_legacy_feed(policy, monkeypatch, unavailable):
    import asyncio
    from core import json_content, codex_usage_store, data_cache, gold_feed, news_feed
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    async def no_codex(*args, **kwargs): return None
    monkeypatch.setattr(codex_usage_store, "get_codex_usage", no_codex)
    monkeypatch.setattr(data_cache, "get_group", lambda *args: {})
    monkeypatch.setattr(data_cache, "version", lambda *args: None)
    monkeypatch.setattr(gold_feed, "cached", lambda: {})
    monkeypatch.setattr(news_feed, "items_cached", lambda: pytest.fail("retired feed must not replace empty publication"))
    def publication():
        if unavailable: raise RuntimeError("synthetic snapshot unavailable")
        return {"mode": "status", "text": "", "freshness": "missing", "version": "same-snapshot"}
    monkeypatch.setattr(news_schedule, "news_publication_snapshot", publication)
    monkeypatch.setattr(news_brief, "_load_state", lambda: pytest.fail("do not reread newer body for its version"))
    result = asyncio.run(json_content._generate_ai_usage_content({}, mac="AA:BB:CC:DD:EE:FF"))
    assert result["feed_news"]["mode"] == "status" and result["feed_news"]["text"] == ""
    assert result["feed_versions"]["news"] == ("unavailable" if unavailable else "same-snapshot")
    if unavailable:
        assert result["feed_news"]["issue_status"]["state"] == "unknown"
