# news_calendar.py — 中国法定工作日/调休日历与（仅审计用）旧时刻表（第二阶段升级）
"""- 时区 Asia/Shanghai（+8，无 DST）。
- 法定口径来自本地可配置文件 data/news_calendar.json：
    {"holidays": ["2026-05-01", ...], "workdays": ["2026-02-14", ...], "source": "...", "updated": "..."}
  未配置 → 明确降级（工作日=周一~五）；日期年份超出配置覆盖年份 → 同样降级并如实标注。
- 新三期调度（news_schedule 使用）在工作日 08:55/12:55/16:55、休息日仅 08:55。
- 阶段2b 旧触发（08:30/整半点/20:30/休息日变化触发）已停用：下方 workday_triggers/
  scheduled_for/allowed_window 仅保留供历史测试与审计比对，news_schedule 不再调用。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

_CAL_FILE = Path(__file__).resolve().parent.parent / "data" / "news_calendar.json"
_CAL = None  # 缓存 {holidays:set, workdays:set, source, degraded, years:list}


def configure_calendar_file(path) -> None:
    global _CAL_FILE, _CAL
    _CAL_FILE = Path(path)
    _CAL = None


def _calendar() -> dict:
    global _CAL
    if _CAL is None:
        cfg = {"holidays": set(), "workdays": set(), "source": None,
               "degraded": True, "years": [], "manual_holidays": set(),
               "manual_workdays": set()}
        try:
            d = json.loads(_CAL_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                cfg["holidays"] = {str(x) for x in (d.get("holidays") or [])}
                cfg["workdays"] = {str(x) for x in (d.get("workdays") or [])}
                cfg["source"] = d.get("source") or d.get("note")
                if cfg["holidays"] or cfg["workdays"]:
                    cfg["degraded"] = False
                yrs = sorted({x[:4] for x in cfg["holidays"] | cfg["workdays"]})
                if (d.get("years") or []) and isinstance(d.get("years"), list):
                    yrs = sorted({str(y) for y in d["years"]} | set(yrs))
                cfg["years"] = yrs
        except OSError:
            pass
        except json.JSONDecodeError:
            cfg["degraded"] = True
        try:
            from .operator_config import load_effective
            overrides = (load_effective().config.get("device_policy") or {}).get(
                "calendar_overrides") or {}
            cfg["manual_holidays"] = {str(x) for x in overrides.get("manual_holidays") or []}
            cfg["manual_workdays"] = {str(x) for x in overrides.get("manual_workdays") or []}
        except Exception:
            # Invalid operator config is handled by its own last-known-good
            # loader; the calendar remains safely usable without overrides.
            pass
        _CAL = cfg
    return _CAL


def calendar_status() -> dict:
    c = _calendar()
    if c["degraded"]:
        return {"degraded": True,
                "note": "法定节假日/调休表未配置，工作日=周一至五（降级）",
                "holidays": [], "workdays": [], "years": [],
                "manual_holidays": sorted(c.get("manual_holidays") or []),
                "manual_workdays": sorted(c.get("manual_workdays") or [])}
    return {"degraded": False,
            "note": f"已配置日历（{c['source'] or '自定义'}），覆盖年份 {c['years'] or ['?']}",
            "holidays": sorted(c["holidays"]),
            "workdays": sorted(c["workdays"]),
            "years": c["years"],
            "manual_holidays": sorted(c.get("manual_holidays") or []),
            "manual_workdays": sorted(c.get("manual_workdays") or [])}


def workday_info(d) -> dict:
    """Return the day type together with whether the answer is authoritative.

    Dates outside the configured government-calendar years fall back to the
    Monday-Friday rule and are explicitly marked degraded.  Consumers must not
    present that fallback as an official holiday/workday decision.
    """
    ds = d.strftime("%Y-%m-%d")
    year = str(d.year)
    c = _calendar()
    if ds in c.get("manual_holidays", set()):
        return {"workday": False, "degraded": False, "source": "manual override",
                "covered_years": list(c.get("years") or [])}
    if ds in c.get("manual_workdays", set()):
        return {"workday": True, "degraded": False, "source": "manual override",
                "covered_years": list(c.get("years") or [])}
    degraded = c["degraded"] or year not in c["years"]
    if degraded:
        value = d.weekday() < 5
    elif ds in c["holidays"]:
        value = False
    elif ds in c["workdays"]:
        value = True
    else:
        value = d.weekday() < 5
    return {
        "workday": bool(value),
        "degraded": bool(degraded),
        "source": c.get("source"),
        "covered_years": list(c.get("years") or []),
    }


def is_workday(d: datetime) -> bool:
    """中国法定工作日（调休口径）。
    日期年份超出日历覆盖范围 → 降级按周一~五判断（调用方/日志须如实标注）。"""
    return bool(workday_info(d)["workday"])


def bj_now() -> datetime:
    """当前北京钟面（naive，主机 TZ 无关）：UTC now + 8h。"""
    import time as _t
    import datetime as _dt
    return _dt.datetime.fromtimestamp(_t.time(), tz=_dt.timezone.utc).replace(
        tzinfo=None) + _dt.timedelta(hours=8)


def workday_triggers() -> list[str]:
    """常规触发时刻（HH:MM，工作日）：08:30 + 09:30..18:30 每小时整半点 + 20:30。"""
    times = ["08:30"] + [f"{h:02d}:30" for h in range(9, 19)] + ["20:30"]
    return sorted(set(times))


def scheduled_for(d: datetime) -> list[datetime]:
    """返回当天应发生的计划触发时刻（datetime 列表），调用方按“是否已做”过滤。"""
    if is_workday(d):
        allowed = workday_triggers()
    else:
        allowed = ["08:30"]
    base = datetime(d.year, d.month, d.day)
    out = []
    for t in allowed:
        hh, mm = int(t[:2]), int(t[3:5])
        out.append(base.replace(hour=hh, minute=mm))
    return out


def allowed_window(now: datetime) -> tuple[bool, str, datetime | None]:
    """现在是否处于允许汇总时段；返回 (允许, 说明, 下次允许时刻或 None)。"""
    hm = now.hour * 60 + now.minute
    if is_workday(now):
        if now.hour >= 22 or now.hour < 8:
            return False, "夜间(22:00-08:30)不汇总", None
        # 22:00 边界由 hour>=22 处理
        return True, "workday window", None
    # 休息日
    t830 = 8 * 60 + 30
    t2000 = 20 * 60
    if hm < t830:
        return False, "休息日 08:30 前不汇总", None
    if hm >= t2000:
        return False, "休息日 20:00 后不汇总", None
    return True, "rest-day window(08:30-20:00)", None
