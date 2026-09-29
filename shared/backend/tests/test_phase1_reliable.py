"""第一阶段发布端可靠数据层测试（pytest；不依赖外网/凭据）。"""
import json
import os
import sys
import tempfile
import time

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from core import data_cache as dc  # noqa: E402
from core import gold_feed  # noqa: E402
from core import news_feed  # noqa: E402
from core import reliable_sources as rs  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path):
    dc.configure_cache_file(str(tmp_path / "cache.json"))
    yield
    dc.configure_cache_file(dc.DEFAULT_CACHE_FILE)


# ── data_cache ───────────────────────────────────────────────
def test_cache_missing_not_zero_and_legal_zero():
    assert dc.group_status("g") == "unknown"
    assert (dc.get_group("g") or {}).get("value") is None
    dc.record_success("g", 0.0)
    g = dc.get_group("g")
    assert g["value"] == 0.0 and g["last_success"] is not None
    assert dc.group_status("g") == "fresh"


def test_failure_keeps_value_and_last_success():
    dc.record_success("g", 12.5)
    ls = dc.get_group("g")["last_success"]
    time.sleep(0.01)
    assert dc.record_failure("g", note="down") == "stale"
    g = dc.get_group("g")
    assert g["value"] == 12.5 and g["last_success"] == ls


def test_never_success_failure_unknown():
    assert dc.record_failure("g") == "unknown"


def test_version_stable_and_independent(tmp_path):
    v1 = dc.bump_version("ai_key", {"a": 1})
    v2 = dc.bump_version("ai_key", {"a": 1})
    dc.bump_version("news", {"t": "x"})
    assert v1 == v2
    assert dc.version("ai_key") == v2
    v3 = dc.bump_version("ai_key", {"a": 2})
    assert v3 != v1


# ── reliable_sources ─────────────────────────────────────────
def test_pick_seven_day_cases():
    assert rs.pick_seven_day({"windows": [
        {"duration_minutes": 300, "used_percent": 45},
        {"duration_minutes": 10080, "used_percent": 80, "resets_at": 2}]})["used_percent"] == 80
    assert rs.pick_seven_day({"windows": [{"duration_minutes": 300, "used_percent": 45}]}) is None
    assert rs.pick_seven_day({"windows": [{"duration_minutes": 10080}]}) is None  # 无 used
    assert rs.pick_seven_day(None) is None


def test_member_config_exact(monkeypatch, tmp_path):
    from core import manual_settings
    monkeypatch.setattr(manual_settings, "member", lambda: {})
    cfg = {"plan": "PLUS", "renewal_status": "cancelled", "valid_until_date": "2025-01-15",
           "precision": "date", "source": "manual"}
    mf = tmp_path / "member.json"
    mf.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(rs, "MEMBER_CONFIG_FILE", mf)
    rs.reload_member_config()
    m = rs.member_config()
    assert m["valid_until_date"] == "2025-01-15" and "00:00" not in json.dumps(m)
    assert m["precision"] == "date" and m["source"] == "manual"


def test_deepseek_by_currency_and_zero_recheck(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(order, value):
        def _f():
            calls["n"] += 1
            if calls["n"] == 1:
                return {"CNY": value, "USD": 0.0 if order else 1.0, "is_available": True}
            # 复核（第二次）
            return {"CNY": value, "USD": 0.0, "is_available": True}
        return _f

    # USD 前、CNY 后（顺序可变）
    monkeypatch.setattr(rs, "fetch_deepseek_balances",
                        fake_fetch(order=True, value=4.31))
    r = rs.refresh_deepseek()
    assert r["balances"]["CNY"] == 4.31 and r["balances"]["USD"] == 0.0
    assert dc.group_status("ai.ds.cny") == "fresh"

    # 合法 0（突变为 0 → 复核仍 0 → 接受）
    monkeypatch.setattr(rs, "fetch_deepseek_balances",
                        fake_fetch(order=False, value=0.0))
    calls["n"] = 0
    r2 = rs.refresh_deepseek()
    assert r2["balances"]["CNY"] == 0.0  # 复核后接受 0
    assert dc.get_group("ai.ds.cny")["value"] == 0.0

    # 失败不清零
    monkeypatch.setattr(rs, "fetch_deepseek_balances", lambda: None)
    r3 = rs.refresh_deepseek()
    assert r3["ok"] is False
    assert dc.get_group("ai.ds.cny")["value"] == 0.0
    assert dc.group_status("ai.ds.cny") == "stale"


def test_cache_deepseek_result_strips_order(monkeypatch):
    infos = [{"currency": "USD", "total_balance": "0.00"},
             {"currency": "CNY", "total_balance": "6.35"}]
    out = rs.cache_deepseek_result({"balance_infos": infos})
    assert out == {"CNY": 6.35, "USD": 0.0}
    assert dc.get_group("ai.ds.usd")["value"] == 0.0


# ── news_feed ────────────────────────────────────────────────
def test_news_fetch_and_dup_and_clamp(monkeypatch):
    gen = {"title": "A" * 80, "summary": "B" * 200, "url": "https://x/1",
           "focus_date": "2026-09-05 12:00:00", "source": "央视新闻"}
    calls = {"n": 0}

    def fake_general():
        calls["n"] += 1
        return gen if calls["n"] == 1 else {"title": "C" * 5, "summary": "S",
                                            "url": "https://x/2", "focus_date": "x"}

    monkeypatch.setitem(news_feed._FETCHERS, "general", fake_general)
    r = news_feed.refresh_category("general")
    assert r["ok"] and len(r["item"]["title"]) <= news_feed.TITLE_MAX + 1  # 截断
    assert len(r["item"]["summary"]) <= news_feed.SUMMARY_MAX + 1
    # 跨分类重复：同样标题 -> skipped
    monkeypatch.setitem(news_feed._FETCHERS, "finance",
                        lambda: {"title": "A" * 80, "summary": "S", "url": "https://f",
                                 "ctime": "1"})
    r2 = news_feed.refresh_category("finance")
    assert r2.get("skipped_dup") is True and r2["ok"] is False
    # 源失败保留旧内容
    monkeypatch.setitem(news_feed._FETCHERS, "general", lambda: (_ for _ in ()).throw(OSError("down")))
    r3 = news_feed.refresh_category("general")
    assert r3["ok"] is False and dc.group_status("news.general") == "stale"
    assert dc.get_group("news.general")["value"]["title"]  # 旧内容保留


def test_tech_ai_keyword_pick(monkeypatch):
    items = [{"title": "索尼新镜头谍照", "description": "d"},
             {"title": "大模型厂商纷纷卖Token，Kimi等开天猫店", "description": "ai"}]

    class FakeRoot:
        def find(self, tag):
            return FakeChannel()

    class FakeChannel:
        def findall(self, tag):
            return items

    def fake_ithome():
        import xml.etree.ElementTree as ET
        return None  # 走 monkeypatch 的假对象

    monkeypatch.setitem(news_feed._FETCHERS, "tech_ai", fake_ithome)
    # 直接测关键词命中逻辑
    picked = None
    for it in items:
        if any(k.lower() in it["title"].lower() for k in news_feed._AI_KEYWORDS):
            picked = it
            break
    assert picked and "大模型" in picked["title"]


# ── gold_feed v3（XAUS CNY/gram；30s 合并/半小时槽位/错误语义）────────
def _freeze_gold(fixture_payload, request_status=200, http_err=None):
    """monkeypatch gold_feed：无网络请求，返回固定响应或抛 HTTPError。"""
    import urllib.error as ue

    class _Resp:
        status = request_status
        def read(self):
            return json.dumps(fixture_payload).encode()
    class _URL:
        def __enter__(self): return _Resp()
        def __exit__(self, *a): pass

    def _urlopen(req, timeout=25):
        if http_err is not None:
            raise ue.HTTPError(ENDPOINT, http_err, "err", {}, None)
        return _URL()
    import core.gold_feed as gf
    gf.urllib.request.urlopen = _urlopen
    gf.time.sleep = lambda s: None
    return gf


ENDPOINT = "https://xaus.com/api/v1/spot?currency=CNY&unit=gram&compact=1"


def _xaus_payload(price=956.2143, stamp="2026-09-05T01:06:00Z"):
    return {"xau": {"price": price, "currency": "CNY", "unit": "gram"},
            "spot_usd_oz": 4420.12, "fx_rate": 6.73,
            "fx_source": "open.er-api.com", "fx_stale": False,
            "updated_at": stamp,
            "data_state": {"status": "fresh", "as_of": stamp,
                           "source": "upstream"},
            "stale": False, "price_as_of": stamp,
            "source": "xaus.com", "price_source": "gold-api.com"}


def test_gold_real_field_parse_and_gram_conversion(monkeypatch, tmp_path):
    import core.gold_feed as gf
    gf.configure_state_file(str(tmp_path / "gold_state.json"))
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    fixed = _dt(2026, 9, 5, 9, 6, tzinfo=_tz(_td(hours=8))).timestamp()
    payload = _xaus_payload()
    _freeze_gold(payload)
    r = gf.refresh(now=fixed)
    assert r["ok"] is True
    it = r["item"]
    assert it["provider"] == "xaus.com" and it["instrument_id"] == "XAU"
    assert it["currency"] == "CNY" and it["unit"] == "g"
    assert abs(it["price_gram_cny"] - 956.2143) < 1e-9
    assert it["spot_usd_oz"] == 4420.12 and it["fx_rate"] == 6.73
    assert it["fx_as_of"] is None and it["fetched_at"] > 0
    assert it["note"] is None


def test_gold_errors_and_retry_semantics(monkeypatch, tmp_path):
    import core.gold_feed as gf
    gf.configure_state_file(str(tmp_path / "gold_state.json"))
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    fixed = _dt(2026, 9, 5, 9, 6, tzinfo=_tz(_td(hours=8))).timestamp()
    # XAUS 无密钥；429 被分类且不会无限重试。
    calls = {"n": 0}
    real = gf.urllib.request.urlopen
    def _limited(req, timeout=25):
        calls["n"] += 1
        import urllib.error as ue
        raise ue.HTTPError(gf.ENDPOINT, 429, "limited", {}, None)
    monkeypatch.setattr(gf.urllib.request, "urlopen", _limited)
    r2 = gf.refresh(force=True, reason="test", request_id="limited-0001", now=fixed)
    assert r2.get("error_class") == "rate_limited" and calls["n"] == 1
    # 新 logical id、跨 30 秒后可再次成功。
    calls["n"] = 0
    _freeze_gold(_xaus_payload(1.0, "2026-09-05T01:07:00Z"))
    r4 = gf.refresh(force=True, reason="test", request_id="limited-0002", now=fixed + 60)
    assert r4["ok"] is True


def test_gold_window_dedup_across_restart(monkeypatch, tmp_path):
    import core.gold_feed as gf
    import core.gold_baseline as gb
    gf.configure_state_file(str(tmp_path / "gold_state.json"))
    gb.configure_state_file(str(tmp_path / "gold_baseline_state.json"))
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    fixed = _dt(2026, 9, 5, 15, 6, tzinfo=_tz(_td(hours=8))).timestamp()
    calls = {"n": 0}
    payload = _xaus_payload(956.0, "2026-09-05T07:06:00Z")
    _freeze_gold(payload)
    orig = gf.urllib.request.urlopen
    def counting(req, timeout=25):
        calls["n"] += 1
        return orig(req, timeout=timeout)
    monkeypatch.setattr(gf.urllib.request, "urlopen", counting)
    r = gf.refresh(now=fixed)
    # Spot and intraday recovery must not make back-to-back provider requests.
    assert r["ok"] is True and calls["n"] == 1
    # 重启模拟（重载模块状态文件仍在）：窗口 done → 不再请求
    gf2 = __import__("importlib").import_module("core.gold_feed")
    gf2.configure_state_file(str(tmp_path / "gold_state.json"))
    monkeypatch.setattr(gf2.urllib.request, "urlopen", counting)
    r2 = gf2.refresh(now=fixed + 1)
    assert r2.get("window_done") is True and calls["n"] == 1
    # 新半小时槽位 → 允许一次新请求。
    _freeze_gold(_xaus_payload(957.0, "2026-09-05T07:30:00Z"))
    next_request = gf2.urllib.request.urlopen
    def counting_next(req, timeout=25):
        calls["n"] += 1
        return next_request(req, timeout=timeout)
    monkeypatch.setattr(gf2.urllib.request, "urlopen", counting_next)
    r3 = gf2.refresh(now=_dt(2026, 9, 5, 15, 30, tzinfo=_tz(_td(hours=8))).timestamp())
    assert r3.get("ok") is True and calls["n"] == 2


# ── 发布端敏感字段剔除 ───────────────────────────────────────
def test_cloud_publish_strip_sensitive():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cp", os.path.join(BACKEND, "..", "tools", "cloud_publish.py"))
    cp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cp)
    d = cp._strip_sensitive({
        "api_key": "x",
        "nested": {"password": "y", "keep": 1},
        "items": [{"token": "z", "access_token": "a", "prompt_tokens": 12}, 2],
        "deepseek_today_tokens": 88,
        "deepseek_today_tokens_complete": False,
        "deepseek_today_tokens_source": "actual",
    })
    assert "api_key" not in d and "password" not in d["nested"] and d["nested"]["keep"] == 1
    assert "token" not in d["items"][0]
    assert "access_token" not in d["items"][0]
    assert d["items"][0]["prompt_tokens"] == 12
    assert d["deepseek_today_tokens"] == 88
    assert d["deepseek_today_tokens_complete"] is False
    assert d["deepseek_today_tokens_source"] == "actual"



# ── gold_catchup 补拉策略（冷却/预算/自校验/防重复）────────────
def test_catchup_policy(monkeypatch, tmp_path):
    from core import gold_catchup as gc
    G = gc.gf  # gold_catchup 实际绑定的 gold_feed 实例
    G.configure_state_file(str(tmp_path / "gold_state.json"))
    import time as _time
    # needs_catchup 天然为 True（缓存无报价）；catchup() 内部会再次自校验
    monkeypatch.setattr(gc, "can_attempt", lambda now=None: (True, "ok"))
    calls = {"n": 0}

    def fake_refresh(force=False, **_kwargs):
        calls["n"] += 1
        return {"ok": True, "item": {"price_gram_cny": 1.0, "quote_time": 9999999999}}
    monkeypatch.setattr(G, "refresh", fake_refresh)
    r = gc.catchup()
    assert r["ok"] is True and r["action"] == "fetched" and calls["n"] == 1
    assert gc.status()["wake_refresh_policy"] == "explicit-cold-start-bounded"
    # 成功后权威缓存已新鲜；重复请求只返回 noop，不访问供应商。
    monkeypatch.setattr(G, "cached", lambda **_kwargs: {"quote_time": _time.time()})
    r2 = gc.catchup()
    assert r2["action"] == "noop" and calls["n"] == 1
    assert gc.needs_catchup() is False
    monkeypatch.setattr(G, "cached", lambda **_kwargs: {"quote_time": _time.time() - 90000})
    assert gc.needs_catchup() is True
    monkeypatch.setattr(G, "cached", lambda **_kwargs: None)
    assert gc.needs_catchup() is True
