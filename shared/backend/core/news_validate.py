# news_validate.py — 简报文案发布前校验（第二阶段补充）
"""校验规则（§五/§八）：
- 文案不得含设备字库（fonts_full_charset.txt，即 fonts_gen 全集）不支持的字符 → 拒绝并列出 Unicode。
- 正文 21px（reg21）可见/advance 总宽 ≤ 636px（x136..772）；超宽 → 触发“文案重写”标记，
  不得缩小字体或以省略号常规截断（发布端负责重写；设备端仅越界保护）。
- 无 HTML/换行/控制字符；字段齐全（tag/text/id）。
宽度按 reg21 表 advance（64 单位）求和（比 ink 偏保守，保证 ≤ink 限）。
"""
from __future__ import annotations

import re
from pathlib import Path

_FIRMWARE = Path(__file__).resolve().parent.parent.parent / "firmware"
_CHARSET_FILE = _FIRMWARE / "fonts_full_charset.txt"
_FONT21 = _FIRMWARE / "src" / "fonts_gen" / "misans_reg_21.h"
MAX_TEXT_PX = 636
W = 800

_charset = None
_font = None  # {"unicode": set, "adv": dict}

_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.#'/-]*")
_LATIN_RUN_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9+.#'/-]*(?:[ \t]+[A-Za-z][A-Za-z0-9+.#'/-]*)+"
)
_ENGLISH_PROSE_WORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "has", "have",
    "in", "into", "is", "its", "joins", "launches", "of", "on", "or", "plans",
    "releases", "says", "the", "to", "unveils", "will", "with", "welcomes",
}
_INTERNAL_PREFIX_RE = re.compile(
    r"(?:^|[\n。；])\s*(?:分析|思考|推理过程|内部说明|提示词|"
    r"analysis|reasoning|system\s+prompt)\s*[:：]",
    re.IGNORECASE,
)
_JSONISH_RE = re.compile(
    r"```|</?(?:think|analysis)>|[\{\[]\s*[\"'](?:body|events|reasons|exception)[\"']\s*:",
    re.IGNORECASE,
)


def _load_charset() -> set[int]:
    global _charset
    if _charset is None:
        s = set()
        try:
            for ch in _CHARSET_FILE.read_text(encoding="utf-8"):
                if ch not in "\n\r\t":
                    s.add(ord(ch))
        except OSError:
            pass
        _charset = s
    return _charset


def _load_font21():
    global _font
    if _font is not None:
        return _font
    unis, advs = set(), {}
    try:
        txt = _FONT21.read_text(encoding="utf-8")
        import re as _re
        def arr(name):
            m = _re.search(r"static const u?int(?:16|32)_t " + name + r"\[\] PROGMEM = \{(.*?)\};", txt, _re.S)
            if not m:
                return []
            return [int(v, 0) for v in _re.split(r"[,\s]+", m.group(1).strip()) if v]
        u = arr("font_misans_reg_21_unicode")
        a = arr("font_misans_reg_21_adv")
        if u and len(u) == len(a):
            unis = set(u)
            advs = {cp: adv for cp, adv in zip(u, a)}
    except OSError:
        pass
    _font = {"unicode": unis, "adv": advs}
    return _font


def unsupported_chars(text: str) -> list[str]:
    """返回不在设备字库的字符（黑块风险 → 拒绝）。"""
    allowed = _load_charset()
    return sorted({ch for ch in text if ord(ch) not in allowed and not ch.isspace()})


def width_px21(text: str) -> int:
    """reg21 advance 总宽（px，保守≥可见边界）。缺失字形按 12px 记。"""
    f = _load_font21()
    total = 0
    for ch in text:
        cp = ord(ch)
        adv = f["adv"].get(cp)
        if adv is None:
            # ASCII 常见半宽兜底
            total += 12 * 64 if cp > 0x2FFF else 10 * 64
        else:
            total += adv
    return (total + 63) // 64  # adv 为 1/64 单位 → px


def validate_visible_chinese(text: str, *, min_cjk: int = 6) -> dict:
    """Reject English prose and leaked model structure while allowing product names.

    Brand/model fragments such as ``OpenAI``, ``GPT-5 API`` and
    ``Apple Vision Pro`` remain legal inside a Chinese sentence.  We reject
    pure-English copy, English clause-like runs containing common prose words,
    and analysis/JSON/prompt leakage.  This is intentionally separate from the
    font/width gate so every final publication path can apply both checks.
    """
    value = re.sub(r"[ \t\u3000]+", " ", str(text or "")).strip()
    if not value:
        return {"ok": False, "reason": "language-empty", "cjk": 0, "latin_words": 0}
    if _INTERNAL_PREFIX_RE.search(value):
        return {"ok": False, "reason": "internal-analysis-leak", "cjk": 0,
                "latin_words": len(_LATIN_TOKEN_RE.findall(value))}
    if _JSONISH_RE.search(value):
        return {"ok": False, "reason": "json-or-prompt-leak", "cjk": 0,
                "latin_words": len(_LATIN_TOKEN_RE.findall(value))}
    cjk = len(re.findall(r"[\u3400-\u9fff]", value))
    latin_words = _LATIN_TOKEN_RE.findall(value)
    for run in _LATIN_RUN_RE.findall(value):
        tokens = _LATIN_TOKEN_RE.findall(run)
        lowered = {token.lower().strip("'-") for token in tokens}
        if len(tokens) >= 3 and lowered & _ENGLISH_PROSE_WORDS:
            return {"ok": False, "reason": "english-clause", "cjk": cjk,
                    "latin_words": len(latin_words), "latin_run": run[:80]}
    if latin_words and cjk < int(min_cjk):
        return {"ok": False, "reason": "not-natural-chinese", "cjk": cjk,
                "latin_words": len(latin_words)}
    latin_letters = len(re.findall(r"[A-Za-z]", value))
    if latin_letters > max(64, cjk * 3):
        return {"ok": False, "reason": "english-dominant", "cjk": cjk,
                "latin_words": len(latin_words)}
    return {"ok": True, "reason": "", "cjk": cjk,
            "latin_words": len(latin_words)}


def validate_line(text: str) -> dict:
    """返回 {ok, width_px, missing, needs_rewrite, reason}。"""
    if not text:
        return {"ok": False, "width_px": 0, "missing": [], "needs_rewrite": True,
                "reason": "empty"}
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\n\r]", text):
        return {"ok": False, "width_px": 0, "missing": [], "needs_rewrite": True,
                "reason": "control/newline"}
    if "<" in text and ">" in text:
        return {"ok": False, "width_px": 0, "missing": [], "needs_rewrite": True,
                "reason": "html-like"}
    missing = unsupported_chars(text)
    w = width_px21(text)
    ok = (not missing) and (w <= MAX_TEXT_PX)
    return {"ok": ok, "width_px": w, "missing": missing,
            "needs_rewrite": not ok,
            "reason": ("missing glyphs " + ",".join(f"U+{ord(c):04X}" for c in missing[:5]))
            if missing else ("" if w <= MAX_TEXT_PX else f"width {w}>636")}


def validate_brief(items: list[dict]) -> dict:
    """校验三条完整简报。"""
    rows = []
    ok_all = True
    for i, it in enumerate(items or []):
        tag = str(it.get("tag") or "")
        text = str(it.get("text") or "")
        v = validate_line(text)
        language = validate_visible_chinese(text)
        tv = validate_line(tag) if tag else {"ok": True, "width_px": 0, "missing": [], "needs_rewrite": False, "reason": ""}
        rows.append({"i": i, "id": it.get("id"), "tag": tag, "text": text,
                     "tag_ok": tv["ok"], "text_ok": v["ok"],
                     "width_px": v["width_px"], "missing": v["missing"],
                     "language_ok": language["ok"],
                     "language_reason": language["reason"],
                     "needs_rewrite": v["needs_rewrite"] or not language["ok"]})
        ok_all = ok_all and tv["ok"] and v["ok"] and language["ok"]
    return {"ok": ok_all, "rows": rows}
