# date_table.py — 农历/节气/传统节日日期表（二阶段补充；发布端确定性计算，不用模型）
"""供给右上日期栏：公历 MM-DD + 周X + 农历月日 + 节气 + 传统节日。

历法：
- 依赖 lunar-python 1.4.8，避免 sxtwl 2.0.7 在 macOS ARM64 上缺少可安装制品；本项目只生成运行所需日期表。
- 时区按 Asia/Shanghai：节气取“该节气在北京时间所属公历日整天显示”。
- 关键日期用官方资料交叉验证（国办 2026 节假日通知：除夕=2026-02-16 腊月廿九、春节=02-17 正月初一、
  中秋=2026-09-25 八月十五 等），并在测试中断言。
- 节/日规则（首版）：正月初一春节、正月十五元宵、五月初五端午、七月初七七夕、七月十五中元、
  八月十五中秋、九月初九重阳、腊月初八腊八、腊月廿三北小年、腊月廿四南小年；
  除夕 = “下一农历年正月初一”的公历前一天（支持腊月廿九除夕）；闰月不机械重复普通月节日；
  清明按节气显示（节日表不含清明，避免重复）。
- 行文本按公共宽度修剪规则：公历星期 → 节日及节气 → 普通农历月日；先删农历，再删分隔；不截断/不省略号。
- 年份超出日期表覆盖范围 → 调用方降级为“公历+星期”（本模块返回 None，由上层记录“农历不可用”）。

对外：
- build_rows(solar_start: date, solar_end: date) -> list[dict]
    [{d:"YYYY-MM-DD", w:"周一".., m:"腊月廿九", q:"", f:"除夕", a:"02-16 周一 · 除夕 · 腊月廿九",
      b:"02-16 周一 · 除夕"}]
    a=完整（农历+节/气，宽度允许）；b=删农历（节/气保留）；f 与 q 同天可共存（“除夕 · 大雪”…）。
- today_row(rows, today) 帮助查找。
"""
from __future__ import annotations

import datetime as _dt
import logging

logger = logging.getLogger(__name__)

try:
    from lunar_python import Lunar, Solar
    HAS_LUNAR_PYTHON = True
except Exception:  # noqa: BLE001
    Lunar = Solar = None
    HAS_LUNAR_PYTHON = False

_MONTH_NAMES = {1: "正月", 2: "二月", 3: "三月", 4: "四月", 5: "五月", 6: "六月",
                7: "七月", 8: "八月", 9: "九月", 10: "十月", 11: "冬月", 12: "腊月"}
_WEEK_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_D1 = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]

# 节日表（非闰月；农历月,日 -> 名称）
FESTIVALS = {(1, 1): "春节", (1, 15): "元宵", (5, 5): "端午", (7, 7): "七夕",
             (7, 15): "中元", (8, 15): "中秋", (9, 9): "重阳",
             (12, 8): "腊八", (12, 23): "北小年", (12, 24): "南小年"}

SUPPORTED_YEARS = (2025, 2026, 2027)   # 发布端生成窗口覆盖年份（表本身只生成运行所需区间）
SOURCE = "lunar-python-1.4.8；节日规则见本文件首版约定"
DEP = "lunar-python==1.4.8（backend venv）"


def lunar_month_day_name(month: int, day: int, leap: bool) -> str:
    """“腊月廿九”/“闰六月初三”。month: 1..12, day: 1..30。"""
    if month == 12 and day == 29 and False:  # 占位防误用（除夕另算）
        pass
    m = _MONTH_NAMES.get(month)
    if not m:
        return ""
    if day <= 10:
        d = "初" + _D1[day] if day < 10 else "初十"
    elif day < 20:
        d = "十" + _D1[day % 10]
    elif day == 20:
        d = "二十"
    elif day < 30:
        d = "廿" + _D1[day % 10]
    else:
        d = "三十"
    return ("闰" if leap else "") + m + d


def _next_lunar_new_year_solar(lyear: int) -> _dt.date | None:
    """农历 lyear 的春节（正月初一）公历日期。lyear 为农历年。"""
    if not HAS_LUNAR_PYTHON:
        return None
    try:
        d = Lunar.fromYmd(lyear, 1, 1).getSolar()
        return _dt.date(d.getYear(), d.getMonth(), d.getDay())
    except Exception as e:  # noqa: BLE001
        logger.warning("[DATE_TABLE] fromLunar(%s,1,1) failed: %s", lyear, e)
        return None


def _lunar_of(solar: _dt.date):
    """返回 (农历年, 月, 日, 是否闰月) 或 None（超范围/库缺失）。"""
    if not HAS_LUNAR_PYTHON:
        return None
    try:
        d = Solar.fromYmd(solar.year, solar.month, solar.day).getLunar()
        month = d.getMonth()
        return (d.getYear(), abs(month), d.getDay(), month < 0)
    except Exception as e:  # noqa: BLE001
        logger.warning("[DATE_TABLE] fromSolar(%s) failed: %s", solar, e)
        return None


def _term_of(solar: _dt.date) -> str:
    if not HAS_LUNAR_PYTHON:
        return ""
    try:
        return Solar.fromYmd(solar.year, solar.month, solar.day).getLunar().getJieQi() or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def _festival_of(solar: _dt.date) -> str:
    """首版传统节日；除夕=下一农历年正月初一前一天。
    腊月日期归属农历年 ly，下一农历春节为 fromLunar(ly+1,1,1)。"""
    lu = _lunar_of(solar)
    if lu is None:
        return ""
    ly, lm, ld, leap = lu
    if not leap:
        f = FESTIVALS.get((lm, ld))
        if f:
            return f
    nxt = _next_lunar_new_year_solar(ly + 1)
    if nxt is not None and (nxt - _dt.timedelta(days=1)) == solar:
        return "除夕"
    return ""


def _base(solar: _dt.date, show_weekday: bool = True) -> str:
    base = f"{solar.month:02d}-{solar.day:02d}"
    return f"{base} {_WEEK_NAMES[solar.weekday()]}" if show_weekday else base


def compose_row_text(solar: _dt.date, fit: bool, width_fn=None, max_w: int = 560,
                     *, show_weekday: bool = True, show_lunar: bool = True,
                     show_solar_terms: bool = True, show_festivals: bool = True) -> str:
    """拼日期栏文本。
    组合顺序（按规格 4.2-4.5）：
    - 仅节日/仅节气：公历星期 · 农历月日 · 节日(或节气)（空间不足删农历）
    - 节日与节气同日：公历星期 · 节日 · 节气（隐藏农历）
    - 无节/气：公历星期 · 农历月日
    fit=False 时一律隐藏农历（b 候选）；width_fn(s)->px 宽度判断，None 用启发值。"""
    lu = _lunar_of(solar)
    lunar = lunar_month_day_name(lu[1], lu[2], lu[3]) if lu and show_lunar else ""
    f = _festival_of(solar) if show_festivals else ""
    q = _term_of(solar) if show_solar_terms else ""

    def w(s: str) -> int:
        if width_fn:
            return width_fn(s)
        return sum(21 if ord(c) > 0x2E7F else (7 if c == " " else 10) for c in s)

    base = _base(solar, show_weekday=show_weekday)
    cands = []
    if fit:
        if f and q:                      # 节日与节气同日：隐藏农历（4.4）
            cands = [[base, f, q], [base, f], [base]]
        elif f:
            cands = [[base, lunar, f] if lunar else [base, f],
                     [base, f], [base]]
        elif q:
            cands = [[base, lunar, q] if lunar else [base, q],
                     [base, q], [base]]
        else:
            cands = [[base, lunar] if lunar else [base], [base]]
    else:
        if f and q:
            cands = [[base, f, q], [base, f], [base]]
        elif f:
            cands = [[base, f], [base]]
        elif q:
            cands = [[base, q], [base]]
        else:
            cands = [[base]]
    for parts in cands:
        s = " · ".join(parts)
        if w(s) <= max_w:
            return s
    return base


def build_rows(start: _dt.date, end: _dt.date, max_w: int = 560, width_fn=None,
               *, preferences: dict | None = None) -> list[dict]:
    """生成 [start,end] 日期表行（含 a=完整、b=删农历 两候选文本）。"""
    rows = []
    preferences = preferences or {}
    flags = {
        "show_weekday": preferences.get("show_weekday", True),
        "show_lunar": preferences.get("show_lunar", True),
        "show_solar_terms": preferences.get("show_solar_terms", True),
        "show_festivals": preferences.get("show_festivals", True),
    }
    d = start
    while d <= end:
        lu = _lunar_of(d)
        lunar = lunar_month_day_name(lu[1], lu[2], lu[3]) if lu else ""
        a = compose_row_text(d, fit=True, width_fn=width_fn, max_w=max_w, **flags)
        b = compose_row_text(d, fit=False, width_fn=width_fn, max_w=max_w, **flags)
        rows.append({"d": d.strftime("%Y-%m-%d"), "w": _WEEK_NAMES[d.weekday()],
                     "m": lunar, "q": _term_of(d), "f": _festival_of(d),
                     "a": a, "b": b})
        d += _dt.timedelta(days=1)
    return rows


def pick_row(rows: list[dict], solar: _dt.date) -> dict | None:
    ds = solar.strftime("%Y-%m-%d")
    for r in rows:
        if r["d"] == ds:
            return r
    return None


# ── 发布端载荷（screen 级，不含于 AI/news 版本）────────────────
PUBLIC_LEFT = 28
PUBLIC_RIGHT = 772
TITLE_GAP = 16
_HDR = {"reg21": "misans_reg_21.h", "bold30": "misans_bold_30.h"}
_adv_cache: dict[str, dict[int, int]] = {}


def _load_adv(style_size: str) -> dict[int, int]:
    if style_size in _adv_cache:
        return _adv_cache[style_size]
    import re as _re
    from pathlib import Path
    fw = Path(__file__).resolve().parent.parent.parent / "firmware" / "src" / "fonts_gen"
    name = f"font_misans_{style_size}"
    txt = (fw / _HDR[style_size]).read_text(encoding="utf-8")
    def arr(typ, suffix):
        m = _re.search(r"static const " + typ + r" " + name + suffix + r"\[\] PROGMEM = \{(.*?)\};", txt, _re.S)
        return [int(v, 0) for v in _re.split(r"[,\s]+", m.group(1).strip()) if v] if m else []
    uni = arr("uint32_t", "_unicode")
    adv = arr("uint16_t", "_adv")
    d = {cp: a for cp, a in zip(uni, adv)}
    _adv_cache[style_size] = d
    return d


def text_width_px(style_size: str, s: str) -> int:
    adv = _load_adv(style_size)
    total = 0
    for ch in s:
        total += adv.get(ord(ch), 21 * 64)
    return (total + 63) // 64


def date_bar_budget() -> int:
    """公共日期栏最大宽度：772−28−16−max(两页标题 Bold30 ink 宽)。与固件/主机同算式。"""
    w_title = max(text_width_px("bold30", "AI 用量"), text_width_px("bold30", "今日关注"))
    return PUBLIC_RIGHT - PUBLIC_LEFT - TITLE_GAP - w_title


def build_table_payload(days_past: int = 3, days_future: int = 92,
                        preferences: dict | None = None) -> dict:
    """生成 screen.calendar：连续多日表（今天±），a/b 两候选按真实字形宽度修剪到公共预算。"""
    today = _bj_today()
    start = today - _dt.timedelta(days=days_past)
    end = today + _dt.timedelta(days=days_future)
    budget = date_bar_budget()
    rows = build_rows(start, end, max_w=budget,
                      width_fn=lambda s: text_width_px("reg21", s),
                      preferences=preferences)
    workday_status = {"degraded": True, "note": "法定工作日日历不可用", "years": []}
    try:
        from . import news_calendar as _workcal
        workday_status = _workcal.calendar_status()
        for row in rows:
            day = _dt.datetime.strptime(row["d"], "%Y-%m-%d").date()
            info = _workcal.workday_info(day)
            row["workday"] = info["workday"]
            row["workday_degraded"] = info["degraded"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DATE_TABLE] workday attach failed: %s", exc)
        for row in rows:
            day = _dt.datetime.strptime(row["d"], "%Y-%m-%d").date()
            row["workday"] = day.weekday() < 5
            row["workday_degraded"] = True
    return {"tz": "Asia/Shanghai", "source": SOURCE, "dep": DEP,
            "supported_years": list(SUPPORTED_YEARS),
            "range": [start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")],
            "budget": budget, "preferences": dict(preferences or {}),
            "workday_calendar": workday_status, "days": rows}


def _bj_today() -> _dt.date:
    import time as _t
    utc = _dt.datetime.fromtimestamp(_t.time(), tz=_dt.timezone.utc).replace(tzinfo=None)
    return (utc + _dt.timedelta(hours=8)).date()
