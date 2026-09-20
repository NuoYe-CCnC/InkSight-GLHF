"""Structured payload (JSON v1) builder for device-side rendering.

把模式 JSON 定义 + 已生成的内容（content dict）翻译为设备端可执行的
白名单 widget 列表（接口契约 §1.5）。不支持的 mode_id 返回 None，
调用方回退到 BMP 通道。
v1.1：AI_USAGE 输出 modules（模块+区域定位，§1.5.2）；所有 payload 带 payload_id
（值稳定哈希，供设备端"无变化跳过刷屏"）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

from .activity_signals import (
    activity_key, activity_observed_at, ai_visual_key,
    news_gold_visual_key,
)

logger = logging.getLogger(__name__)

SCHEMA_VER = 1


def _payload_id(persona: str, content: dict) -> str:
    """值稳定标识：相同显示值 → 相同 id；值变化 → id 变化。用于设备端跳过刷屏。"""
    content = content or {}
    if persona == "AI_USAGE":
        key = [
            content.get("deepseek_balance"),
            content.get("deepseek_currency"),
            [(w.get("label"), w.get("used_percent")) for w in (content.get("codex_windows") or [])],
            content.get("codex_source"),
            content.get("codex_stale"),
            # feed_ai 全量参与版本：到期列表等变化必须改变 payload_id（触发设备重绘）
            content.get("feed_ai"),
        ]
    else:
        skip = {"updated_at", "ts", "generated_at", "time", "date_str", "weather_str",
                "weather", "date"}
        key = {k: v for k, v in content.items() if k not in skip}
    raw = json.dumps(key, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def _text(text: str, font: str = "gb2312_16", max_lines: int = 3, align: str = "left",
          _id: str = "") -> dict:
    w = {"type": "text", "font": font, "text": text or "", "max_lines": max_lines, "align": align}
    if _id:
        w["id"] = _id
    return w


def _big_number(text: str, unit: str = "", font: str = "ascii_5x7_x8", _id: str = "") -> dict:
    w = {"type": "big_number", "font": font, "text": str(text or ""), "unit": unit}
    if _id:
        w["id"] = _id
    return w


def _separator() -> dict:
    return {"type": "separator", "style": "solid"}


def _spacer(height: int = 6) -> dict:
    return {"type": "spacer", "height": height}


def _icon_text(icon: str, text: str, font: str = "gb2312_16", _id: str = "") -> dict:
    w = {"type": "icon_text", "icon": icon, "text": text or "", "font": font}
    if _id:
        w["id"] = _id
    return w


# ── MEMO ────────────────────────────────────────────────────
def _build_memo(content: dict, w: int, h: int) -> list[dict]:
    out = []
    for i in (1, 2, 3):
        title = content.get(f"memo_title_{i}")
        body = content.get(f"memo_text_{i}")
        if not body:
            continue
        if title:
            out.append(_text(title, font="gb2312_24", max_lines=1, _id=f"title_{i}"))
        out.append(_text(body, font="gb2312_16", max_lines=10 if h > 400 else 3, _id=f"body_{i}"))
        out.append(_spacer(4))
    if not out:
        return []
    return out


# ── WEATHER ─────────────────────────────────────────────────
_WEATHER_ICON = {0: "wx_sunny", 1: "wx_cloudy", 2: "wx_overcast", 3: "wx_rain",
                 4: "wx_rain", 5: "wx_thunder", 6: "wx_snow", 7: "wx_fog"}


def _build_weather(content: dict, w: int, h: int) -> list[dict]:
    out = []
    city = content.get("city") or "—"
    out.append(_text(city, font="gb2312_24", max_lines=1, _id="city"))
    out.append(_spacer(4))
    out.append(_big_number(content.get("today_temp", "--"), unit="°C", _id="temp"))
    code = content.get("today_code", -1)
    icon = _WEATHER_ICON.get(int(code) if isinstance(code, (int, float)) else -1, "wx_cloudy")
    out.append(_icon_text(icon, content.get("today_desc") or "", _id="desc"))
    out.append(_spacer(2))
    if content.get("today_range"):
        out.append(_text(content["today_range"], font="gb2312_16", max_lines=1, _id="range"))
    if content.get("today_humidity"):
        out.append(_text(f"湿度 {content.get('today_humidity')}%   {content.get('today_wind_dir','')} {content.get('today_wind_level','')}",
                         font="gb2312_16", max_lines=1, _id="humidity"))
    out.append(_separator())
    forecast = content.get("forecast") or []
    if forecast:
        items = []
        for day in forecast[:4]:
            if not isinstance(day, dict):
                continue
            items.append({
                "day": day.get("day") or day.get("date") or "",
                "code": int(day.get("code", -1)) if isinstance(day.get("code"), (int, float)) else -1,
                "hi": day.get("hi") or day.get("high") or "--",
                "lo": day.get("lo") or day.get("low") or "--",
            })
        if items:
            out.append({"type": "forecast_row", "id": "forecast", "items": items})
    if content.get("advice"):
        out.append(_text(content["advice"], font="gb2312_16", max_lines=2, _id="advice"))
    return out


# ── COUNTDOWN ───────────────────────────────────────────────
def _build_countdown(content: dict, w: int, h: int) -> list[dict]:
    out = []
    if content.get("message"):
        out.append(_text(content["message"], font="gb2312_16", max_lines=2, _id="msg"))
    events = content.get("events") or content.get("countdownEvents") or []
    if isinstance(events, dict):
        events = [events]
    for idx, ev in enumerate(events[:4]):
        if not isinstance(ev, dict):
            continue
        name = ev.get("name") or ""
        days = ev.get("days")
        if name:
            out.append(_text(name, font="gb2312_16", max_lines=1, _id=f"ev_name_{idx}"))
        if days is not None:
            out.append(_big_number(days, unit="天", _id=f"ev_days_{idx}"))
        if ev.get("date"):
            out.append(_text(ev["date"], font="gb2312_16", max_lines=1, _id=f"ev_date_{idx}"))
        out.append(_spacer(4))
    return out


# ── AI_USAGE ────────────────────────────────────────────────
def _build_ai_usage(content: dict, w: int, h: int) -> list[dict]:
    out = []
    total = content.get("deepseek_balance") or "--"
    currency = content.get("deepseek_currency") or "CNY"
    out.append(_text("DeepSeek", font="misans_24", max_lines=1, _id="ds_title"))
    out.append(_big_number(total, unit=currency, _id="ds_balance"))
    out.append(_text(
        f"可用 {'是' if content.get('deepseek_available') else '否'} · "
        f"充值 {content.get('deepseek_topped_up') or '--'} · 赠送 {content.get('deepseek_granted') or '--'}",
        font="misans_16", max_lines=2, _id="ds_extra"))
    out.append(_separator())
    out.append(_text("Codex", font="misans_24", max_lines=1, _id="cx_title"))
    windows = content.get("codex_windows") or []
    if windows:
        first = windows[0]
        used0 = first.get("used_percent")
        rem0 = first.get("remaining_pct")
        if rem0 is not None:
            # 突出剩余量：大数字 = 剩余 %（与 DeepSeek 余额同款）
            out.append(_big_number(f"{rem0:.0f}", unit="%", _id="cx_remaining"))
        cap = []
        if first.get("label"):
            cap.append(f"{first['label']} 窗口")
        if used0 is not None:
            cap.append(f"已用 {used0:.0f}%")
        r0 = first.get("resets_at")
        if r0:
            try:
                from datetime import datetime, timedelta, timezone
                d = datetime.fromtimestamp(float(r0), tz=timezone(timedelta(hours=8)))
                cap.append(f"重置 {d.strftime('%m-%d %H:%M')}")
            except (ValueError, TypeError, OSError):
                pass
        if cap:
            out.append(_text(" · ".join(cap), font="misans_16", max_lines=1, _id="cx_cap"))
        for idx, win in enumerate(windows[:4]):
            used = win.get("used_percent")
            label = win.get("label") or ""
            if used is None:
                continue
            out.append({
                "type": "progress", "id": f"cx_{idx}", "percent": float(used),
                "label": f"{label} 已用 {used:.0f}%",
            })
    else:
        out.append(_text("暂无额度数据", font="misans_16", max_lines=1, _id="cx_empty"))
    _meta = content.get("codex_meta") or "来源 本机"
    out.append(_text(_meta, font="misans_16", max_lines=1, _id="cx_meta"))
    return out


def _build_ai_usage_modules(content: dict, w: int, h: int) -> list[dict]:
    """AI_USAGE 模块化布局：左卡 DeepSeek，右卡 Codex（§1.5.2）。"""
    margin = 16
    top = 16
    gap = 12
    left_w = max(200, int(w * 45 / 100))
    right_w = w - left_w - gap - margin * 2
    mod_h = h - top - 24
    if right_w < 200 or mod_h < 100:
        return []
    ds_widgets = [wd for wd in _build_ai_usage(content, w, h) if wd.get("id", "").startswith(("ds_",))][:3]
    cx_widgets = [wd for wd in _build_ai_usage(content, w, h) if wd.get("id", "").startswith(("cx_",))]
    return [
        {"id": "ds", "region": {"x": margin, "y": top, "w": left_w, "h": mod_h},
         "widgets": ds_widgets or [{"type": "text", "font": "misans_16", "text": "DeepSeek 无数据", "max_lines": 2}]},
        {"id": "cx", "region": {"x": margin + left_w + gap, "y": top, "w": right_w, "h": mod_h},
         "widgets": cx_widgets or [{"type": "text", "font": "misans_16", "text": "Codex 无数据", "max_lines": 2}]},
    ]


# ── HOTLIST（全网热点榜单）─────────────────────────────────
def _build_hotlist(content: dict, w: int, h: int) -> list[dict]:
    out = []
    items = content.get("items") or []
    for it in items[:12]:
        if not isinstance(it, dict):
            continue
        title = it.get("title")
        if not title:
            continue
        rank = it.get("rank", "")
        hot = it.get("hot", "")
        line = f"{rank}. {title}"
        if hot not in ("", None):
            line += f"  {hot}"
        out.append(_text(line, font="misans_16", max_lines=1, _id=f"hot_{rank}"))
    if not out:
        out.append(_text("暂无热点数据", font="misans_16", max_lines=1, _id="hot_empty"))
    return out


_BUILDERS = {
    "MEMO": _build_memo,
    "WEATHER": _build_weather,
    "COUNTDOWN": _build_countdown,
    "AI_USAGE": _build_ai_usage,
    "HOTLIST": _build_hotlist,
}


def build_screen_payload(persona: str, content: dict, w: int, h: int,
                         mode_def: Optional[dict] = None,
                         caps: Optional[dict] = None) -> Optional[dict]:
    """返回 JSON v1 screen payload；模式不受支持时返回 None。"""
    persona = (persona or "").upper()
    builder = _BUILDERS.get(persona)
    if builder is None:
        logger.info("[STRUCTURED] mode %s not yet supported, falling back to BMP", persona)
        return None
    try:
        widgets = builder(content or {}, int(w), int(h))
    except Exception:
        logger.exception("[STRUCTURED] widget build failed for %s", persona)
        return None
    if not widgets:
        logger.info("[STRUCTURED] empty widgets for %s, falling back to BMP", persona)
        return None
    footer_label = None
    if mode_def and isinstance(mode_def, dict):
        footer = (mode_def.get("layout") or {}).get("footer")
        if isinstance(footer, dict):
            footer_label = footer.get("label")
    screen = {
        "mode_id": persona,
        "layout_mode": "full",
        "refresh": "auto",
        "payload_id": _payload_id(persona, content),
        "widgets": widgets,
        "footer": {"label": footer_label or persona},
    }
    if persona == "AI_USAGE":
        try:
            from .operator_config import load_effective as _load_operator
            _operator = _load_operator().config
        except Exception as exc:  # noqa: BLE001
            logger.warning("[STRUCTURED] operator policy attach degraded to defaults: %s", exc)
            _operator = {}
        display_preferences = dict(_operator.get("panel_display") or {})
        device_policy = dict(_operator.get("device_policy") or {})
        device_policy["gold_refresh"] = {
            "wake_enabled": bool((_operator.get("gold_refresh") or {}).get("wake_enabled", True))
        }
        news_policy = dict(device_policy.get("news_check") or {})
        news_cfg = dict(_operator.get("news_digest") or {})
        news_policy["enabled"] = bool(news_policy.get("enabled", True)
                                      and news_cfg.get("enabled", True))
        news_policy["schedules"] = [
            {"id": str(row.get("id") or ""),
             "time": str(row.get("time") or ""),
             "enabled": bool(row.get("enabled", False)),
             "day_types": list(row.get("day_types") or [])}
            for row in (news_cfg.get("schedules") or [])
            if isinstance(row, dict)
        ]
        device_policy["news_check"] = news_policy
        modules = _build_ai_usage_modules(content, int(w), int(h))
        if modules:
            screen["modules"] = modules
            screen["widgets"] = []
        # 第二阶段：双面板扩展（AI 页面渲染字段不变；新闻/金价页数据+版本）
        screen["panels"] = {"ai": True,
                            "news_gold": bool(content.get("feed_news") or content.get("feed_gold"))}
        screen["layout_version"] = int(content.get("layout_version") or 1)
        screen["font_version"] = str(content.get("font_version") or "legacy")
        screen["device_policy"] = device_policy
        screen["display_preferences"] = display_preferences
        feed_ai = content.get("feed_ai") or {}
        feed_ai_for_visual = dict(feed_ai)
        # The device renders the screen-level effective preferences. Key the
        # refresh decision from that exact same view, not a possibly older
        # content snapshot.
        feed_ai_for_visual["display_preferences"] = display_preferences
        feed_ng = {
            "news": content.get("feed_news") or {},
            "gold": content.get("feed_gold"),
        }
        act_key = activity_key(feed_ai)
        observed_at = activity_observed_at(feed_ai)
        screen["versions"] = {
            "ai_key": content.get("feed_versions", {}).get("ai_key"),
            "activity_key": act_key,
            "activity_valid": bool(act_key and observed_at),
            "activity_observed_at": observed_at,
            "ai_visual_key": ai_visual_key(feed_ai_for_visual),
            "news_gold_visual_key": news_gold_visual_key(feed_ai, feed_ng),
            "news": content.get("feed_versions", {}).get("news"),
            "gold": content.get("feed_versions", {}).get("gold"),
        }
        screen["pages"] = {
            "ai": feed_ai,
            "news_gold": feed_ng,
        }
        # 日期表（农历/节气/节日）：screen 级附加；不参与 payload_id 与 AI/news 版本
        try:
            from .date_table import build_table_payload as _build_cal
            screen["calendar"] = _build_cal(preferences=display_preferences)
            screen["versions"]["calendar_key"] = hashlib.sha256(
                json.dumps(screen["calendar"], ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:20]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[STRUCTURED] calendar attach failed: %s", exc)
    return {
        "schema_ver": SCHEMA_VER,
        "payload_type": "screen",
        "screen": screen,
    }
