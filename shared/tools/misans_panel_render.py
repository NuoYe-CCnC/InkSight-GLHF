#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""misans_panel_render.py v2（2026-09-07 排版修复版）—— MiSans 双面板主机渲染器

用途：
  1) 产出两张参考 PNG（AI / 资讯+金价），冻结 SHA；
  2) 产出与设备 imgBuf 同构的 48000B 帧（1=白 0=黑，MSB-first，800x480）供 XOR；
  3) --layout-report：字段级布局报告（每字段 字体/字号/baseline/ink 边界/左右对齐
     误差/与相邻字段间距/是否截断/缺字 Unicode），对照修复版布局规则校验。

实现逐条镜像 shared/firmware/src/panel_pages.cpp（2026-09-07 修复版）与
panel_ui.cpp（ink 边界量宽右对齐）。字形来自固件实际编译的 fonts_gen/*.h。
时间按设备 configTime(+8h) 口径：lt8(now) 显示北京时刻；页脚“更新”用 doc 顶层 ts。

用法：
  python misans_panel_render.py ai|newsgold --doc payload.json --now <epoch>
      [--png out.png] [--raw out.bin] [--layout-report out.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
from pathlib import Path

W, H = 800, 480
ROW_BYTES = W // 8
FB_LEN = ROW_BYTES * H
FONT_DIR = Path(__file__).resolve().parent.parent / "firmware" / "src" / "fonts_gen"
TZ = 8 * 3600


def _arr(header: str, tag: str, field: str) -> list[int]:
    m = re.search(r"static const (?:u?int(?:8|16|32)_t|int16_t) " + tag + r"_" + field + r"\[\] PROGMEM = \{(.*?)\};", header, re.S)
    if not m:
        raise ValueError(f"{tag}_{field} 数组未找到")
    return [int(v, 0) for v in re.split(r"[,\s]+", m.group(1).strip()) if v]


class MiFont:
    def __init__(self, short: str, full: str, header: str):
        self.tag = short          # 显示用（ops.font）
        self.unicode = _arr(header, full, "unicode")
        self.adv = _arr(header, full, "adv")
        self.x0 = _arr(header, full, "x0")
        self.top = _arr(header, full, "top")
        self.iw = _arr(header, full, "iw")
        self.ih = _arr(header, full, "ih")
        off = _arr(header, full, "off")
        m = re.search(r"static const uint8_t " + full + r"_data\[\] PROGMEM = \{(.*?)\};", header, re.S)
        self.data = bytes(int(v, 0) for v in re.split(r"[,\s]+", m.group(1).strip()) if v)
        self.off = off
        self.count = len(self.unicode)
        assert len(self.off) == self.count and (self.unicode == sorted(self.unicode))

    def glyph_index(self, cp: int) -> int:
        lo, hi = 0, self.count - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            v = self.unicode[mid]
            if v == cp:
                return mid
            if v < cp:
                lo = mid + 1
            else:
                hi = mid - 1
        return -1


def load_fonts() -> dict:
    fonts = {}
    for style, sizes in (("reg", (16, 21, 24, 30, 62, 82)), ("bold", (24, 30, 62, 82))):
        for sz in sizes:
            short = f"{style}{sz}"
            full = f"font_misans_{style}_{sz}"
            fonts[short] = MiFont(short, full, (FONT_DIR / f"misans_{style}_{sz}.h").read_text(encoding="utf-8"))
    return fonts


def _iter_cp(s: str):
    b = s.encode("utf-8")
    i, n = 0, len(b)
    while i < n:
        c = b[i]
        if c < 0x80:
            yield c, 1
            i += 1
        elif (c & 0xE0) == 0xC0 and i + 1 < n:
            yield (((c & 0x1F) << 6) | (b[i + 1] & 0x3F)), 2
            i += 2
        elif (c & 0xF0) == 0xE0 and i + 2 < n:
            yield (((c & 0x0F) << 12) | ((b[i + 1] & 0x3F) << 6) | (b[i + 2] & 0x3F)), 3
            i += 3
        else:
            yield 0xFFFD, 1
            i += 1


class Panel:
    def __init__(self):
        self.fb = bytearray(b"\xff" * FB_LEN)
        self.ops = []          # 字段级绘制记录
        self.missing: list[int] = []
        self._logged: set[int] = set()

    def _resolve(self, f: MiFont, cp: int) -> int:
        idx = f.glyph_index(cp)
        if idx < 0:
            if cp not in self._logged:
                self._logged.add(cp)
                if len(self.missing) < 40:
                    self.missing.append(cp)
            idx = f.glyph_index(0x25A0)
        return idx

    def set_black(self, x: int, y: int):
        if 0 <= x < W and 0 <= y < H:
            self.fb[y * ROW_BYTES + x // 8] &= ~(0x80 >> (x % 8))

    def fill_rect(self, x: int, y: int, w: int, h: int):
        for py in range(max(0, y), min(H, y + h)):
            for px in range(max(0, x), min(W, x + w)):
                self.fb[py * ROW_BYTES + px // 8] &= ~(0x80 >> (px % 8))

    def hline(self, x0: int, x1: int, y: int, thickness: int):
        if x1 < x0:
            x0, x1 = x1, x0
        self.fill_rect(x0, y, x1 - x0 + 1, thickness)

    def vline(self, x: int, y0: int, y1: int, thickness: int):
        if y1 < y0:
            y0, y1 = y1, y0
        self.fill_rect(x, y0, thickness, y1 - y0 + 1)

    def rect_outline(self, x: int, y: int, w: int, h: int, thickness: int):
        self.hline(x, x + w - 1, y, thickness)
        self.hline(x, x + w - 1, y + h - thickness, thickness)
        self.vline(x, y, y + h - 1, thickness)
        self.vline(x + w - thickness, y, y + h - 1, thickness)

    # ── 度量（镜像 panel_ui.cpp）──────────────────────────
    def text_width(self, f: MiFont, s: str) -> int:
        w = 0
        for cp, _ in _iter_cp(s):
            if cp == 0xFFFD:
                w += 12 * 64
                continue
            idx = f.glyph_index(cp)
            if idx < 0:
                idx = f.glyph_index(0x25A0)
            w += f.adv[idx] if idx >= 0 else 12 * 64
        return (w + 31) // 32

    def ink_right64(self, f: MiFont, s: str) -> int:
        pen = 0
        right = 0
        for cp, _ in _iter_cp(s):
            if cp == 0xFFFD:
                pen += 12 * 64
                continue
            idx = self._resolve(f, cp)
            if idx < 0:
                pen += 12 * 64
                continue
            iw = f.iw[idx]
            if iw > 0:
                right = pen + (f.x0[idx] + iw) * 64
            pen += f.adv[idx]
        return right

    def ink_width(self, f: MiFont, s: str) -> int:
        return (self.ink_right64(f, s) + 63) // 64

    def visible_left_offset(self, f: MiFont, s: str) -> int:
        """Actual first black pixel relative to the pen origin."""
        pen = 0
        left = None
        for cp, _ in _iter_cp(s):
            idx = self._resolve(f, cp)
            if idx < 0:
                pen += 12 * 64
                continue
            iw, ih = f.iw[idx], f.ih[idx]
            if iw > 0 and ih > 0:
                row_bytes = (iw + 7) // 8
                off = f.off[idx]
                for row in range(ih):
                    data = f.data[off + row * row_bytes: off + (row + 1) * row_bytes]
                    for col in range(iw):
                        if not (data[col // 8] & (0x80 >> (col % 8))):
                            actual = pen // 64 + f.x0[idx] + col
                            left = actual if left is None else min(left, actual)
                            break
            pen += f.adv[idx]
        return int(left or 0)

    def blit_glyph(self, f: MiFont, idx: int, pen_x: int, base_y: int) -> int:
        iw, ih = f.iw[idx], f.ih[idx]
        if iw <= 0 or ih <= 0:
            return f.adv[idx]
        row_bytes = (iw + 7) // 8
        off = f.off[idx]
        sx, sy = pen_x + f.x0[idx], base_y - f.top[idx]
        for r in range(ih):
            yy = sy + r
            if yy < 0 or yy >= H:
                continue
            row = f.data[off + r * row_bytes: off + (r + 1) * row_bytes]
            for c in range(iw):
                xx = sx + c
                if 0 <= xx < W and not (row[c // 8] & (0x80 >> (c % 8))):
                    self.fb[yy * ROW_BYTES + xx // 8] &= ~(0x80 >> (xx % 8))
        return f.adv[idx]

    def _ink_span(self, f: MiFont, s: str, x: int, base_y: int):
        pen = x * 64
        ink = None
        for cp, _ in _iter_cp(s):
            if cp == 0xFFFD:
                pen += 12 * 64
                continue
            idx = self._resolve(f, cp)
            if idx < 0:
                pen += 12 * 64
                continue
            iw, ih = f.iw[idx], f.ih[idx]
            if iw > 0 and ih > 0:
                l = (pen // 64) + f.x0[idx]
                r = l + iw
                top = base_y - f.top[idx]
                bot = top + ih
                if ink is None:
                    ink = [l, r, top, bot]
                else:
                    ink[0] = min(ink[0], l)
                    ink[1] = max(ink[1], r)
                    ink[2] = min(ink[2], top)
                    ink[3] = max(ink[3], bot)
            pen += f.adv[idx]
        return ink

    def draw_text(self, f: MiFont, s: str, x: int, base_y: int, rec: bool = True):
        pen = x * 64
        for cp, _ in _iter_cp(s):
            if cp == 0xFFFD:
                pen += 12 * 64
                continue
            idx = self._resolve(f, cp)
            if idx < 0:
                pen += 12 * 64
                continue
            pen += self.blit_glyph(f, idx, pen // 64, base_y)
        if rec and s:
            span = self._ink_span(f, s, x, base_y)
            self.ops.append({"op": "text", "font": f.tag, "text": s, "x": x, "baseline": base_y,
                             "ink": span})

    def draw_right(self, f: MiFont, s: str, right_x: int, base_y: int, rec: bool = True):
        x = right_x - self.ink_width(f, s)
        self.draw_text(f, s, x, base_y, False)
        if rec and s:
            span = self._ink_span(f, s, x, base_y)
            self.ops.append({"op": "textr", "font": f.tag, "text": s, "rightX": right_x,
                             "baseline": base_y, "ink": span})

    def draw_trunc(self, f: MiFont, s: str, x: int, base_y: int, max_w: int) -> bool:
        if self.ink_width(f, s) <= max_w:
            self.draw_text(f, s, x, base_y)
            return False
        ell = self.ink_width(f, "…")
        if ell < 0 or ell > max_w:
            ell = max_w
        kept = ""
        for cp, _ in _iter_cp(s):
            cand = kept + chr(cp)
            if self.ink_width(f, cand) > max_w - ell:
                break
            kept = cand
        self.draw_text(f, kept + "…", x, base_y)
        return True


def fmt_money(v: float) -> str:
    if v != v:
        return "--"
    return f"{v:.2f}"


def fmt_signed_delta(v: float) -> str:
    if v != v:
        return "--"
    if abs(v) < 0.005:
        return "0.00"
    return f"{v:+.2f}" if v > 0 else f"{v:.2f}"


def draw_gold_price_delta(p: Panel, fonts: dict, price: str, delta: str,
                          left_x: int, right_limit: int, baseline: int) -> None:
    """Mirror firmware ink-bound layout for one price and its signed delta."""
    tiny = fonts["reg16"]
    candidates = [fonts["reg30"], tiny]
    delta_w = p.ink_width(tiny, delta)
    price_max_w = right_limit - left_x - delta_w - 12
    if price_max_w < 1:
        return
    font = candidates[pick_fit(candidates, p, price, price_max_w)]
    price_w = p.ink_width(font, price)
    if price_w <= price_max_w:
        p.draw_text(font, price, left_x, baseline)
        p.draw_text(tiny, delta, left_x + price_w + 12, baseline)
        return
    p.draw_trunc(font, price, left_x, baseline, price_max_w)
    p.draw_text(tiny, delta, right_limit - delta_w, baseline)


def fmt_compact_tokens(value, complete: bool = True) -> str:
    if value is None:
        return "--"
    try:
        tokens = int(value)
    except (TypeError, ValueError):
        return "--"
    if tokens < 0:
        return "--"
    suffix = "" if complete else "+"
    if tokens < 1000:
        return f"{tokens}{suffix}"

    divisors = (1_000, 1_000_000, 1_000_000_000)
    units = ("k", "M", "B")
    unit = 0 if tokens < divisors[1] else (1 if tokens < divisors[2] else 2)
    scaled = tokens / divisors[unit]
    decimals = 2 if scaled < 10 else (1 if scaled < 100 else 0)
    rounded = scaled
    for _ in range(2):
        factor = 10 ** decimals
        rounded = math.floor(scaled * factor + 0.5) / factor
        adjusted = 2 if rounded < 10 else (1 if rounded < 100 else 0)
        if adjusted == decimals:
            break
        decimals = adjusted
    if rounded >= 1000 and unit < 2:
        unit += 1
        scaled = tokens / divisors[unit]
        decimals = 2 if scaled < 10 else (1 if scaled < 100 else 0)
        for _ in range(2):
            factor = 10 ** decimals
            rounded = math.floor(scaled * factor + 0.5) / factor
            adjusted = 2 if rounded < 10 else (1 if rounded < 100 else 0)
            if adjusted == decimals:
                break
            decimals = adjusted
    return f"{rounded:.{decimals}f}{units[unit]}{suffix}"


def fmt_full_tokens(value, complete: bool = True) -> str:
    """News-page formatter: preserve the full measured integer, including 0."""
    if value is None or isinstance(value, bool):
        return "--"
    try:
        tokens = int(value)
    except (TypeError, ValueError, OverflowError):
        return "--"
    if tokens < 0:
        return "--"
    return f"{tokens}{'' if complete else '+'}"


def jnum(v) -> float:
    if v is None or isinstance(v, bool):
        return math.nan
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return math.nan
    return math.nan


def lt8(epoch: int):
    return time.gmtime(int(epoch) + TZ)


def fmt_md_hm(t: int) -> str:
    # 注意：Python struct_time.tm_mon 为 1–12（与 C 的 0-based 不同），勿 +1
    tm = lt8(t)
    if tm.tm_year >= 123:
        return f"{tm.tm_mon:02d}-{tm.tm_mday:02d} {tm.tm_hour:02d}:{tm.tm_min:02d}"
    return "--"


def fmt_hm(t: int) -> str:
    tm = lt8(t)
    if tm.tm_year >= 123:
        return f"{tm.tm_hour:02d}:{tm.tm_min:02d}"
    return "--:--"


def pick_fit(cands, p: Panel, txt: str, max_w: int) -> int:
    for i, f in enumerate(cands):
        if p.ink_width(f, txt) <= max_w:
            return i
    return len(cands) - 1


def _pages(doc: dict) -> dict:
    return (doc.get("screen") or {}).get("pages") or doc.get("pages") or {}


def payload_ts(doc: dict) -> int:
    return int(doc.get("ts") or 0)


PUB_L, PUB_R, PUB_TOP_Y, PUB_HDR_Y = 24, 778, 39, 50
PUB_FTR_Y, PUB_FOOT_Y = 427, 461
WEEK_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]  # Python struct_time tm_wday(0=周一)


def _bjtm(now: int):
    import time as _t
    return _t.gmtime(int(now) + TZ)  # 北京钟面（naive 语义，与 lt8 一致）


def time_trusted(now: int) -> bool:
    tm = _bjtm(now)
    return tm.tm_year >= 2023   # Python tm_year 为完整年份（C 是 -1900）


def top_date_text(doc: dict, now: int, p) -> str:
    """右上日期栏（镜像固件 topDateText）。p 用于 ink 宽度（reg21）。"""
    if not time_trusted(now):
        return "日期待校准"
    tm = _bjtm(now)
    base = f"{tm.tm_mon:02d}-{tm.tm_mday:02d} {WEEK_CN[tm.tm_wday]}"
    cal = ((doc.get("screen") or {}).get("calendar")) or {}
    budget = int(cal.get("budget") or 560)
    days = cal.get("days") or []
    ymd = f"{tm.tm_year:04d}-{tm.tm_mon:02d}-{tm.tm_mday:02d}"
    f21 = _F21()
    for row in days:
        if row.get("d") == ymd:
            a, b = row.get("a") or "", row.get("b") or ""
            if a and p.ink_width(f21, a) <= budget:
                return a
            if b and p.ink_width(f21, b) <= budget:
                return b
            return base
    return base


_F21_CACHE = {}


def _F21():
    if not _F21_CACHE:
        _F21_CACHE["f"] = MiFont("reg21", "font_misans_reg_21",
                                 (FONT_DIR / "misans_reg_21.h").read_text(encoding="utf-8"))
    return _F21_CACHE["f"]


def _src_word(missing: bool, last: int, now_s: int, stale_after: int) -> str:
    if missing:
        return "missing"
    if last and stale_after and (now_s - last) > stale_after:
        return "stale"
    return "ok"


def page_status_text(doc: dict, page: str, now: int) -> str:
    """统一页脚状态（镜像固件 pageStatusText，§8.5）。"""
    if not time_trusted(now):
        return "时间待校准"
    now_s = int(now)
    ai = _pages(doc).get("ai") or {}
    ng = ((doc.get("screen") or {}).get("pages") or {}).get("news_gold") or {}
    ts = int(doc.get("ts") or 0)
    whole_missing = ts <= 0
    whole_future = (not whole_missing) and ts > now_s + 300
    whole_stale = (not whole_missing and not whole_future) and (now_s - ts) > 7200
    fr = ai.get("fresh") or {}
    gd = ng.get("gold") or {}
    nw = ng.get("news") or {}
    miss = [False] * 8
    last = [0] * 8
    policy = ai.get("freshness_policy") or {}
    def stale_after(source: str, default: int) -> int:
        row = policy.get(source) or {}
        try:
            value = int(row.get("stale_after_s") or default)
        except (TypeError, ValueError):
            value = default
        return value if 1 <= value <= 7 * 86400 else default
    ST = [stale_after("codex", 1800), stale_after("ds_cny", 180),
          stale_after("ds_usd", 180), 0, 0, 0, 0, 24 * 3600]
    miss[0] = ai.get("codex_7d_used") is None
    last[0] = int(fr.get("codex") or 0)
    ds = ai.get("deepseek") or {}
    miss[1] = ds.get("CNY") is None
    last[1] = int(fr.get("ds_cny") or 0)
    miss[2] = ds.get("USD") is None
    last[2] = int(fr.get("ds_usd") or 0)
    miss[3] = not (ai.get("plan") and ai.get("valid_until_date"))
    last[3] = int(fr.get("member") or 0)
    stale_ov = [False] * 8
    mode = str(nw.get("mode") or "")
    if mode in {"digest", "daily_message", "status"}:
        if not (nw.get("text") or ""):
            miss[4] = True
        elif nw.get("freshness") == "error":
            miss[4] = True
        elif nw.get("freshness") == "stale":
            stale_ov[4] = True     # 期次错过（§8.4）：存在但陈旧 → 部分数据陈旧
        gen = int(nw.get("generated_at") or 0)
        last[4] = gen
        miss[5] = gen <= 0
    else:
        items = nw.get("items")
        if isinstance(items, list) and items:
            for it in items:
                if not (isinstance(it, dict) and (it.get("text") or "")):
                    miss[4] = True
        else:
            for key in ("general", "tech_ai", "finance"):
                it = nw.get(key) or {}
                if not it or not (it.get("title") or ""):
                    miss[4] = True
    if gd:
        if (gd.get("price_gram_cny") is None or gd.get("spot_usd_oz") is None
                or gd.get("fx_rate") is None or gd.get("price_as_of") is None):
            miss[7] = True
        else:
            last[7] = int(gd.get("price_as_of") or 0)
        state = gd.get("data_state") or {}
        if gd.get("stale") or gd.get("fx_stale") or (state.get("status") not in (None, "fresh")):
            stale_ov[7] = True
        elif last[7] > 0:
            gt, nt = _bjtm(last[7]), _bjtm(now_s)
            if (gt.tm_year, gt.tm_yday) != (nt.tm_year, nt.tm_yday):
                stale_ov[7] = True
    else:
        miss[7] = True
    use = [True, True, True, True, False, False, False, False]
    if page == "news_gold":
        use[4] = use[5] = use[7] = True
    has_missing = has_stale = False
    for i in range(8):
        if not use[i]:
            continue
        if stale_ov[i]:
            has_stale = True
            continue
        w = _src_word(miss[i], last[i], now_s, ST[i])
        if w == "missing":
            has_missing = True
        elif w == "stale":
            has_stale = True
    if page == "news_gold":
        local_update = str(nw.get("device_update_state") or "")
        local_until = int(nw.get("device_update_expires_at") or 0)
        if local_update == "requested" and local_until >= now_s:
            return "早报待更新"
        if local_update == "unreachable" and local_until >= now_s:
            return "资讯暂未更新"
        update_state = str(nw.get("update_state") or "")
        if update_state == "due":
            return "早报待更新"
        if update_state == "not-updated":
            return "资讯暂未更新"
    if whole_missing:
        return "部分数据缺失" if has_missing else "部分数据陈旧"
    if whole_future:
        return "数据陈旧" if has_stale else "部分数据陈旧"
    if whole_stale:
        return "数据陈旧"
    if has_stale and has_missing:
        return "部分数据陈旧"
    if has_stale:
        return "部分数据陈旧"
    if has_missing:
        return "部分数据缺失"
    return "数据正常"


def fmt_upd_text(status: str, ts: int, now: int) -> str:
    """页脚左文案（镜像 fmtUpdText）：跨日加日期。"""
    if ts > 0:
        tm = _bjtm(ts)
        if tm.tm_year >= 123:
            tn = _bjtm(now)
            same = (tn.tm_year, tn.tm_mon, tn.tm_mday) == (tm.tm_year, tm.tm_mon, tm.tm_mday)
            if same:
                return f"{status} · 更新 {tm.tm_hour:02d}:{tm.tm_min:02d}"
            return f"{status} · 更新 {tm.tm_mon:02d}-{tm.tm_mday:02d} {tm.tm_hour:02d}:{tm.tm_min:02d}"
    return f"{status} · 更新 --:--"


def ai_status_text(doc: dict, now: int) -> str:
    ai = _pages(doc).get("ai") or {}
    miss = 0
    if not (ai.get("plan") or ""):
        miss += 1
    if not (ai.get("valid_until_date") or ""):
        miss += 1
    if ai.get("codex_7d_used") is None:
        miss += 1
    ds = ai.get("deepseek") or {}
    if ds.get("CNY") is None and ds.get("USD") is None:
        miss += 1
    if ai.get("reset_credits_available") is None:
        miss += 1
    ts = payload_ts(doc)
    if ts > 0 and (now - ts) > 7200:
        return "数据陈旧"
    return "部分数据缺失" if miss > 0 else "数据正常"


def footer_left(status: str, ts: int) -> str:
    return f"{status} · 更新 {fmt_hm(ts) if ts > 0 else '--:--'}"


def _credit_value(value, *, unlimited: bool = False) -> str:
    if unlimited:
        return "不限量"
    if value is None:
        return "--"
    try:
        from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "--"
    if not number.is_finite() or number < 0:
        return "--"
    return format(number, "f")


def _display_preferences(doc: dict, ai: dict) -> dict:
    out = {}
    screen = doc.get("screen") if isinstance(doc.get("screen"), dict) else {}
    for source in (screen.get("display_preferences"), ai.get("display_preferences")):
        if isinstance(source, dict):
            out.update(source)
    # Acceptance fixtures historically put the two switches directly on ai.
    for key in ("show_codex_credits", "show_openai_api_info", "show_reset_opportunities"):
        if key in ai:
            out[key] = ai[key]
    return out


def _known_reset_credits(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return int(number)


def _balance_number(value, *, currency: str | None = None,
                    allow_negative: bool = False) -> str:
    if isinstance(value, bool) or value is None:
        return "--"
    try:
        from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
        exact = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "--"
    if not exact.is_finite() or (exact < 0 and not allow_negative):
        return "--"
    if abs(exact) >= Decimal("1e12"):
        body = f"约 {exact:.3e}"
    elif currency:
        body = format(exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
    else:
        try:
            number = float(exact)
        except (OverflowError, ValueError):
            return "--"
        if not math.isfinite(number):
            return "--"
        if number.is_integer():
            body = f"{int(number):,}"
        else:
            body = f"{number:,.2f}"
    return body + (f" {currency}" if currency else "")


def lower_account_rows(ai: dict) -> list[tuple[str, str]]:
    """Zero-reset fallback with the same two automatic values as the switches."""
    return [
        ("API 本月消费", _balance_number(ai.get("openai_api_month_spend_usd"),
                                         currency="USD", allow_negative=True)),
        ("点数余额", _credit_value(ai.get("codex_credit_balance"),
                                   unlimited=ai.get("codex_credit_unlimited") is True)),
    ]


def optional_account_rows(ai: dict) -> list[tuple[str, str]]:
    """Optional account rows used by acceptance previews.

    These fields mirror the user switches without changing the default approved
    layout. Both values are automatic; legacy manual balances are ignored.
    """
    rows = []
    if bool(ai.get("show_codex_credits", False)):
        rows.append(("点数余额", _credit_value(
            ai.get("codex_credit_balance"),
            unlimited=ai.get("codex_credit_unlimited") is True)))
    if bool(ai.get("show_openai_api_info", False)):
        spend = ai.get("openai_api_month_spend_usd")
        value = _balance_number(spend, currency="USD", allow_negative=True)
        rows.append(("API 本月消费", value))
    return rows


# ── AI 面板（镜像 panel_pages.cpp 修复版）───────────────────
def render_ai_panel(p: Panel, fonts: dict, doc: dict, now: int) -> None:
    ai = _pages(doc).get("ai") or {}
    display_prefs = _display_preferences(doc, ai)
    show_codex = bool(display_prefs.get("show_codex_credits", False))
    show_api = bool(display_prefs.get("show_openai_api_info", False))
    show_reset = bool(display_prefs.get("show_reset_opportunities", True))
    F_TITLE, F_LABEL, F_SMALL, F_TINY = fonts["bold30"], fonts["reg24"], fonts["reg21"], fonts["reg16"]
    cny_x0, codex_right, ds_left, page_right = 24, 461, 500, 778

    p.fb = bytearray(b"\xff" * FB_LEN)
    p.ops = []
    dbar = top_date_text(doc, now, p)
    p.draw_text(F_TITLE, "AI 用量", PUB_L, PUB_TOP_Y)
    p.draw_right(F_SMALL, dbar, PUB_R, PUB_TOP_Y)
    p.hline(PUB_L, PUB_R, PUB_HDR_Y, 2)
    p.draw_text(F_TITLE, "CODEX", cny_x0, 93)
    p.draw_text(F_TITLE, "DEEPSEEK", ds_left, 93)
    plan = ai.get("plan") or "会员"
    valid = ai.get("valid_until_date") or ""
    mmdd = valid[5:10] if len(valid) >= 10 else valid
    if len(valid) < 10:
        mmdd = mmdd[:5]
    # 验收示意图可显式给出会员状态文案；正式数据未提供时仍保持原逻辑。
    s = ai.get("membership_display") or f"{plan} · 有效至 {mmdd}"
    if p.ink_width(F_SMALL, s) <= (codex_right - cny_x0 - 16):
        p.draw_right(F_SMALL, s, codex_right, 93)
    else:
        p.draw_right(F_TINY, s, codex_right, 93)

    used_raw = ai.get("codex_7d_used")
    used = jnum(used_raw) if used_raw is not None else -1.0
    used_ok = 0.0 <= used <= 100.0  # 未知/非数值/越界 → 不绘误导比例
    cx_txt = f"{100.0 - used:.0f}%" if used_ok else "--%"
    ds_cny = jnum((ai.get("deepseek") or {}).get("CNY")) if "CNY" in (ai.get("deepseek") or {}) else math.nan
    ds_txt = "--" if ds_cny != ds_cny else fmt_money(ds_cny)
    mains = [fonts["reg82"], fonts["reg62"], fonts["reg30"]]
    cny_w = p.ink_width(F_LABEL, "CNY")
    ds_limit = (page_right - cny_w - 12) - ds_left
    cx_limit = codex_right - cny_x0
    fi = 0
    while fi < len(mains) - 1:
        if p.ink_width(mains[fi], cx_txt) <= cx_limit and p.ink_width(mains[fi], ds_txt) <= ds_limit:
            break
        fi += 1
    p.draw_text(mains[fi], cx_txt, cny_x0, 191)
    p.draw_text(mains[fi], ds_txt, ds_left, 191)
    p.draw_right(F_LABEL, "CNY", page_right, 191)

    # 可选账户信息占用百分比右侧、会员有效期下方的留白。
    # 每项分为“名称 / 数值”两行，并与会员有效期统一右缘，避免横排拥挤。
    account_ai = dict(ai)
    account_ai.update(show_codex_credits=show_codex, show_openai_api_info=show_api)
    account_rows = optional_account_rows(account_ai)
    if len(account_rows) == 1:
        # 单额度版与左侧“剩余 / 7 日窗口”形成两行基线，字号也完全一致。
        label, value = account_rows[0]
        p.draw_right(F_LABEL, label, codex_right, 225)
        # 英数字在同基线下的 ink 底边比中文高 1px，因此下移 1px 做光学底边对齐。
        p.draw_right(F_LABEL, value, codex_right, 257)
    else:
        # 双额度版：API 在上、Codex 在下，以留白完成分组。
        ordered_rows = sorted(account_rows, key=lambda row: 0 if row[0].startswith("API") else 1)
        for (label_y, value_y), (label, value) in zip(
                [(161, 192), (225, 257)], ordered_rows):
            p.draw_right(F_LABEL, label, codex_right, label_y)
            p.draw_right(F_LABEL, value, codex_right, value_y)

    p.draw_text(F_LABEL, "剩余", cny_x0, 225)
    p.draw_text(F_LABEL, "可用余额", ds_left, 240)
    p.draw_text(F_LABEL, "7 日窗口", cny_x0, 256)
    p.vline(480, 62, 415, 2)
    p.hline(ds_left, page_right, 270, 2)
    p.hline(cny_x0, codex_right, 343, 2)
    p.rect_outline(cny_x0, 270, codex_right - cny_x0, 26, 2)
    if used_ok:
        rem = 100.0 - used
        rem = 0.0 if rem < 0 else (100.0 if rem > 100 else rem)
        fill = int((codex_right - cny_x0 - 4) * rem / 100.0)
        if fill > 0:
            p.fill_rect(cny_x0 + 2, 272, fill, 22)

    used_txt = f"已用 {used:.0f}%" if used_ok else "已用 --%"
    p.draw_text(F_LABEL, used_txt, cny_x0, 328)
    ra = int(ai.get("codex_resets_at") or 0)
    reset_txt = f"自动重置 {fmt_md_hm(ra)}" if ra > 0 else "自动重置 --"
    used_right = cny_x0 + p.ink_width(F_LABEL, used_txt)
    if codex_right - p.ink_width(F_SMALL, reset_txt) - used_right >= 16:
        p.draw_right(F_SMALL, reset_txt, codex_right, 328)
    else:
        p.draw_right(F_TINY, reset_txt, codex_right, 328)

    ds = ai.get("deepseek") or {}
    ds_usd = jnum(ds.get("USD")) if "USD" in ds else math.nan
    m1 = fmt_money(ds_cny)
    m2 = fmt_money(ds_usd)
    cur1 = "" if ds_cny != ds_cny else " CNY"
    cur2 = "" if ds_usd != ds_usd else " USD"
    v1 = m1 + cur1
    v2 = m2 + cur2
    p.draw_text(F_LABEL, "人民币余额", ds_left, 315)
    if ds_left + p.ink_width(F_LABEL, "人民币余额") + 12 <= page_right - p.ink_width(F_LABEL, v1):
        p.draw_right(F_LABEL, v1, page_right, 315)
    else:
        p.draw_right(F_SMALL, v1, page_right, 315)
    p.draw_text(F_LABEL, "美元余额", ds_left, 355)
    if ds_left + p.ink_width(F_LABEL, "美元余额") + 12 <= page_right - p.ink_width(F_LABEL, v2):
        p.draw_right(F_LABEL, v2, page_right, 355)
    else:
        p.draw_right(F_SMALL, v2, page_right, 355)
    token_text = fmt_compact_tokens(ai.get("deepseek_today_tokens"),
                                    bool(ai.get("deepseek_today_tokens_complete", False)))
    token_label = "今日 Token"
    p.draw_text(F_LABEL, token_label, ds_left, 395)
    token_max_w = page_right - (ds_left + p.ink_width(F_LABEL, token_label) + 12)
    token_fonts = [F_LABEL, F_SMALL, F_TINY]
    token_font = token_fonts[pick_fit(token_fonts, p, token_text, token_max_w)]
    p.draw_right(token_font, token_text, page_right, 395)

    credits = _known_reset_credits(ai.get("reset_credits_available"))
    s = f"手动重置 {credits} 次可用" if credits is not None else "手动重置 -- 次可用"
    # 到期行：None=未接通 / []=0次 / 列表升序 MM-DD HH:mm（北京），过期不显示
    exp = ai.get("reset_expiry_list")
    exp_txt = "到期时间暂不可用"
    if isinstance(exp, list):
        if not exp:
            exp_txt = "到期 --"
        else:
            shown, more, parts = 0, False, []
            for e in exp:
                try:
                    e = int(e)
                except (TypeError, ValueError):
                    continue
                if e <= 0 or e <= now:
                    continue
                parts.append(fmt_md_hm(e))
                shown += 1
            if shown == 0:
                exp_txt = "到期 --"
            else:
                body = " / ".join(parts)
                limit = page_right - cny_x0
                # 主机端模拟同款截断：先拼，超宽时裁到最后可容纳项
                while body and p.ink_width(F_SMALL, "到期 " + body) > limit:
                    parts.pop()
                    if not parts:
                        body = ""
                        break
                    body = " / ".join(parts)
                more = bool(parts) and shown > len(parts)
                exp_txt = ("到期 " + body + (" …" if more else "")) if body else "到期 --"
    lower_balances = (credits == 0 and ai.get("reset_credits_confirmed") is True
                      and not show_codex and not show_api)
    if lower_balances:
        target_left = cny_x0
        for (label, value), baseline in zip(lower_account_rows(ai), (377, 407)):
            label_x = target_left - p.visible_left_offset(F_SMALL, label)
            p.draw_text(F_SMALL, label, label_x, baseline)
            label_op = p.ops[-1]
            label_right = (label_op.get("ink") or [target_left, target_left])[1]
            max_value_w = codex_right - label_right - 16
            value_font = F_SMALL if p.ink_width(F_SMALL, value) <= max_value_w else F_TINY
            if p.ink_width(value_font, value) > max_value_w:
                value = "数值过大"
            p.draw_right(value_font, value, codex_right, baseline)
    elif show_reset:
        p.draw_text(F_SMALL, s, cny_x0, 380)
        if p.ink_width(F_SMALL, exp_txt) <= page_right - cny_x0:
            p.draw_text(F_SMALL, exp_txt, cny_x0, 407)
        else:
            p.draw_text(F_TINY, exp_txt, cny_x0, 407)

    # 用户确认版页脚：分隔线 y427；状态与更新时间保留。
    st = page_status_text(doc, "ai", now)
    p.hline(PUB_L, PUB_R, PUB_FTR_Y, 2)
    p.draw_text(F_SMALL, fmt_upd_text(st, payload_ts(doc), now), PUB_L, PUB_FOOT_Y)
    p.draw_right(F_SMALL, "INKSIGHT", PUB_R, PUB_FOOT_Y)


# ── 资讯＋金价面板（镜像修复版）─────────────────────────────
def render_news_gold_panel(p: Panel, fonts: dict, doc: dict, now: int) -> None:
    pages = _pages(doc)
    ng = pages.get("news_gold") or {}
    gold = ng.get("gold") or {}
    news = ng.get("news") or {}
    ai = pages.get("ai") or {}
    F_TITLE, F_LABEL, F_SMALL, F_TINY = fonts["bold30"], fonts["reg24"], fonts["reg21"], fonts["reg16"]
    L, R = PUB_L, PUB_R

    p.fb = bytearray(b"\xff" * FB_LEN)
    p.ops = []
    dbar = top_date_text(doc, now, p)
    p.draw_text(F_TITLE, "今日关注", PUB_L, PUB_TOP_Y)
    p.draw_right(F_SMALL, dbar, PUB_R, PUB_TOP_Y)
    p.hline(PUB_L, PUB_R, PUB_HDR_Y, 2)

    usd_oz = jnum(gold.get("spot_usd_oz")) if "spot_usd_oz" in gold else math.nan
    cny_g = jnum(gold.get("price_gram_cny")) if "price_gram_cny" in gold else math.nan
    fx_rate = jnum(gold.get("fx_rate")) if "fx_rate" in gold else math.nan
    state = gold.get("data_state") or {}
    price_at = int(gold.get("price_as_of") or 0)
    current_day = _bjtm(now) if time_trusted(now) else None
    quote_day = _bjtm(price_at) if price_at > 0 else None
    same_quote_day = bool(current_day and quote_day and
                          (current_day.tm_year, current_day.tm_yday) ==
                          (quote_day.tm_year, quote_day.tm_yday))
    gold_stale = bool(gold.get("stale") or state.get("status") not in (None, "fresh")
                      or (time_trusted(now) and not same_quote_day))
    p.draw_text(F_SMALL, "伦敦金 较旧" if gold_stale else "伦敦金", L, 83)
    CNY_X = 330
    p.draw_text(F_SMALL, "人民币金价", CNY_X, 83)
    p.draw_right(F_TINY, "美元人民币汇率", R, 83)

    usd_text = fmt_money(usd_oz)
    usd_base_at = int(gold.get("baseline_price_as_of") or 0)
    usd_base_day = _bjtm(usd_base_at) if usd_base_at > 0 else None
    usd_today = bool(same_quote_day and usd_base_day and
                     (current_day.tm_year, current_day.tm_yday) ==
                     (usd_base_day.tm_year, usd_base_day.tm_yday))
    usd_delta = (jnum(gold.get("change_usd_oz_since_bj_midnight"))
                 if usd_today else math.nan)
    draw_gold_price_delta(p, fonts, usd_text, fmt_signed_delta(usd_delta), L, 282, 124)

    cny_text = fmt_money(cny_g)
    cny_delta = jnum(gold.get("change_cny_g_since_reference"))

    fx_text = fmt_money(fx_rate)
    fx_fonts = [F_SMALL, F_TINY]
    fx_font = fx_fonts[pick_fit(fx_fonts, p, fx_text, 112)]
    p.draw_right(fx_font, fx_text, R, 124)

    p.draw_text(F_TINY, "USD/oz", L, 149)
    p.draw_text(F_TINY, "较北京00:00", 96, 149)
    p.draw_text(F_TINY, "CNY/g", CNY_X, 149)
    cny_kind = str(gold.get("baseline_cny_kind") or "")
    legacy_cny_baseline = gold.get("baseline_cny_g") is not None
    cny_base_at = int(gold.get("baseline_cny_price_as_of") or
                      (gold.get("baseline_price_as_of") if legacy_cny_baseline else 0) or 0)
    cny_base_hm = _bjtm(cny_base_at) if cny_base_at > 0 else None
    cny_today = bool(same_quote_day and cny_kind == "first_valid" and cny_base_hm and
                     (current_day.tm_year, current_day.tm_yday) ==
                     (cny_base_hm.tm_year, cny_base_hm.tm_yday))
    if not cny_today:
        cny_delta = math.nan
    draw_gold_price_delta(p, fonts, cny_text, fmt_signed_delta(cny_delta), CNY_X, 620, 124)
    if cny_today:
        cny_note = f"较今日 {cny_base_hm.tm_hour:02d}:{cny_base_hm.tm_min:02d}"
    else:
        cny_note = "基准待建立"
    p.draw_text(F_TINY, cny_note, CNY_X + 64, 149)
    p.draw_right(F_TINY, "USD/CNY", R, 149)

    base_at = cny_base_at
    quote_hm = _bjtm(price_at) if price_at > 0 else None
    base_hm = _bjtm(base_at) if base_at > 0 else None
    if quote_hm and same_quote_day:
        quote_note = f"报价 {quote_hm.tm_hour:02d}:{quote_hm.tm_min:02d}"
    elif quote_hm:
        quote_note = f"报价 {quote_hm.tm_mon:02d}-{quote_hm.tm_mday:02d} {quote_hm.tm_hour:02d}:{quote_hm.tm_min:02d}"
    else:
        quote_note = "报价 --:--"
    p.draw_text(F_TINY, quote_note, L, 169)
    p.draw_text(F_TINY, f"首笔 {base_hm.tm_hour:02d}:{base_hm.tm_min:02d}" if cny_today else "首笔 --:--", CNY_X, 169)
    fx_cached = bool(gold.get("fx_stale") or gold_stale)
    p.draw_right(F_TINY, "汇率缓存" if fx_cached else "参考汇率", R, 169)
    p.hline(L, R, 174, 2)

    # 资讯中部（三级兼容：digest 段落版 → brief items → 旧三分类）
    mode = str(news.get("mode") or "")
    if mode in {"digest", "daily_message", "status"}:
        dtxt = str(news.get("text") or "")
        if dtxt:
            jname = str(news.get("issue_title") or "") or {
                "noon": "科技 / AI 中报", "evening": "科技 / AI 晚报"}.get(
                    str(news.get("period") or ""), "科技 / AI 早报")
            p.draw_trunc(fonts["bold24"], jname, L, 208, 560)
            gts = int(news.get("generated_at") or 0)
            if gts > 0:
                gm = _bjtm(gts)
                tn = _bjtm(now)
                if gm.tm_year >= 123 and (gm.tm_year, gm.tm_mon, gm.tm_mday) == (
                        tn.tm_year, tn.tm_mon, tn.tm_mday):
                    issue = f"{gm.tm_hour:02d}:{gm.tm_min:02d} 出刊"
                elif gm.tm_year >= 123:
                    issue = f"{gm.tm_mon:02d}-{gm.tm_mday:02d} {gm.tm_hour:02d}:{gm.tm_min:02d} 出刊"
                else:
                    issue = "--:-- 出刊"
            else:
                issue = "--:-- 出刊"
            p.draw_right(F_TINY, issue, R, 208)
            lines = dtxt.split("\n")[:4]
            for k, ln in enumerate(lines):
                if ln:
                    p.draw_text(F_SMALL, ln, L, [241, 273, 305, 337][k])
    else:
        briefs = news.get("items")
        has_briefs = isinstance(briefs, list)
        legacy_keys = ["general", "tech_ai", "finance"]
        legacy_tags = ["综合", "科技AI", "财经"]
        row_base = [216, 270, 324]
        for i in range(3):
            tag, text = "", ""
            if has_briefs and i < len(briefs) and isinstance(briefs[i], dict):
                tag = str(briefs[i].get("tag") or "")
                text = str(briefs[i].get("text") or "")
            elif not has_briefs:
                tag = legacy_tags[i]
                old = news.get(legacy_keys[i]) or {}
                text = str(old.get("title") or "") if isinstance(old, dict) else ""
            if not text:
                continue
            p.draw_text(F_SMALL, tag, L, row_base[i])
            p.draw_trunc(F_SMALL, text, 136, row_base[i], R - 136)

    p.hline(L, R, 350, 2)
    used_raw = ai.get("codex_7d_used")
    used = jnum(used_raw) if used_raw is not None else -1.0
    used_ok = 0.0 <= used <= 100.0
    p.draw_text(fonts["bold24"], "CODEX", L, 381)
    line = f"剩余 {100.0 - used:.0f}%" if used_ok else "剩余 --%"
    p.draw_text(F_LABEL, line, 210, 381)
    ra = int(ai.get("codex_resets_at") or 0)
    line = f"7日 · 重置 {fmt_md_hm(ra)}" if ra > 0 else "7日 · 重置 --"
    p.draw_right(F_SMALL, line, R, 381)

    p.draw_text(fonts["bold24"], "DEEPSEEK", L, 414)
    ds = ai.get("deepseek") or {}
    cny = jnum(ds.get("CNY")) if "CNY" in ds else math.nan
    p.draw_text(F_LABEL, f"{fmt_money(cny)} CNY", 210, 414)
    token_text = fmt_full_tokens(ai.get("deepseek_today_tokens"),
                                 bool(ai.get("deepseek_today_tokens_complete", False)))
    token_line = f"今日 Token · {token_text}"
    amount_right = 210 + p.ink_width(F_LABEL, f"{fmt_money(cny)} CNY")
    token_font = F_SMALL if R - p.ink_width(F_SMALL, token_line) - amount_right >= 16 else F_TINY
    p.draw_right(token_font, token_line, R, 414)

    st = page_status_text(doc, "news_gold", now)
    ts = payload_ts(doc)
    p.hline(PUB_L, PUB_R, PUB_FTR_Y, 2)
    p.draw_text(F_SMALL, fmt_upd_text(st, ts, now), PUB_L, PUB_FOOT_Y)
    p.draw_right(F_SMALL, "INKSIGHT", PUB_R, PUB_FOOT_Y)


def raw_to_png(raw: bytes, out: str):
    from PIL import Image
    im = Image.new("1", (W, H), 1)
    px = im.load()
    for y in range(H):
        base = y * ROW_BYTES
        for x in range(W):
            if not (raw[base + x // 8] & (0x80 >> (x % 8))):
                px[x, y] = 0
    im.save(out)


def layout_checks(p: Panel) -> dict:
    """对照修复版布局规则的字段级检查（与实拍清单对应）。"""
    by = {}
    for op in p.ops:
        by.setdefault(op["text"], []).append(op)
    checks = {}

    def span(op):
        ink = op.get("ink")
        return ink if ink else [op["x"], op["x"], op["baseline"], op["baseline"]]

    # A1 会员行与 CODEX 间距
    codex = by.get("CODEX")
    member = [op for op in p.ops if "有效至" in op["text"]]
    if codex and member:
        c_ink = span(codex[0])
        m_ink = span(member[0])
        checks["A1_codex_member_gap"] = m_ink[0] - c_ink[1]
    # A2 主金额与 CNY 间距（DeepSeek 侧）
    cny_ops = [op for op in p.ops if op["text"] == "CNY" and op["baseline"] == 191]
    if cny_ops:
        c_ink = span(cny_ops[0])
        # 金额 = 同 baseline 左起 dsLeft 的 op
        cands = [op for op in p.ops if op["baseline"] == 191 and op["text"] != "CNY" and op["op"] == "text"]
        if cands:
            big = max(cands, key=lambda o: o["x"])
            b_ink = span(big)
            checks["A2_amount_cny_gap"] = c_ink[0] - b_ink[1]
    # A3 已用与自动重置间距
    used_op = [op for op in p.ops if op["text"].startswith("已用")]
    reset_op = [op for op in p.ops if op["text"].startswith("自动重置")]
    if used_op and reset_op:
        checks["A3_used_reset_gap"] = span(reset_op[0])[0] - span(used_op[0])[1]
    # A4 人民币/美元值右缘是否同一列（≈778）与标签间距
    for label, val in (("人民币余额", None), ("美元余额", None)):
        lop = [op for op in p.ops if op["text"] == label]
        if lop:
            l_ink = span(lop[0])
            # 同 baseline 右对齐值
            vals = [op for op in p.ops if op["op"] == "textr" and op["baseline"] == lop[0]["baseline"]]
            if vals:
                v = max(vals, key=lambda o: o["rightX"])
                v_ink = span(v)
                checks[f"A4_{label}_gap"] = v_ink[0] - l_ink[1]
                checks[f"A4_{label}_value_rightX"] = v.get("rightX")
    # A5 页脚
    foot_ops = [op for op in p.ops if op["baseline"] == 461 or op["baseline"] == 463]
    brands = [op for op in foot_ops if op["text"] == "INKSIGHT"]
    states = [op for op in foot_ops if " · 更新" in op["text"]]
    checks["A5_footer_brand_count"] = len(brands)
    checks["A5_footer_status_present"] = len(states) == 1
    # 缺字
    checks["missing_glyphs"] = [hex(c) for c in sorted(set(p.missing))]
    return checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("panel", choices=("ai", "newsgold"))
    ap.add_argument("--doc", required=True)
    ap.add_argument("--now", type=int, required=True)
    ap.add_argument("--png")
    ap.add_argument("--raw")
    ap.add_argument("--layout-report")
    args = ap.parse_args()

    doc = json.loads(Path(args.doc).read_text(encoding="utf-8"))
    fonts = load_fonts()
    p = Panel()
    if args.panel == "ai":
        render_ai_panel(p, fonts, doc, args.now)
    else:
        render_news_gold_panel(p, fonts, doc, args.now)
    raw = bytes(p.fb)
    assert len(raw) == FB_LEN
    sha = hashlib.sha256(raw).hexdigest()
    if args.raw:
        Path(args.raw).write_bytes(raw)
    if args.png:
        raw_to_png(raw, args.png)
    if args.layout_report:
        rep = {
            "panel": args.panel,
            "doc": Path(args.doc).name,
            "now": args.now,
            "sha256": sha,
            "missing_glyphs": [hex(c) for c in sorted(set(p.missing))],
            "fields": p.ops,
            "checks": layout_checks(p),
        }
        Path(args.layout_report).write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    print(f"sha256={sha} bytes={len(raw)} missing={len(set(p.missing))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
