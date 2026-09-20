#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
font_gen_ttf.py — TTF/OTF 中文字体 → GB2312 点阵 C 数组（MiSans 等）

与 font_gen.py（HZK 位图源）互补：本工具从矢量字体用 FreeType/PIL 光栅化
16x16 / 24x24 点阵，阈值二值化后输出与 imgBuf 位约定一致（1=白，0=黑，MSB-first）。

用法：
  python font_gen_ttf.py --ttf MiSans-Regular.otf --size 16 --out fonts_misans_16.h --name misans_16
  python font_gen_ttf.py --ttf MiSans-Bold.otf   --size 24 --out fonts_misans_24.h --name misans_24
可选 --subset 常用字.txt（UTF-8，去重）。

说明：MiSans 版权归小米（免费商用，需保留许可声明，不得单独转售字体文件；
官方页面 https://hyperos.mi.com/font/ ）。生成产物为字形位图，属衍生使用。
"""
from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw, ImageFont


def glyph_bitmap(font, ch: str, size: int, threshold: int = 128) -> bytes | None:
    try:
        bbox = font.getbbox(ch)
    except Exception:  # noqa: BLE001
        return None
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    if w <= 0 or h <= 0:
        return bytes((size + 7) // 8 * size)  # 空白字符
    img = Image.new("L", (size, size), 255)
    d = ImageDraw.Draw(img)
    ox = (size - w) // 2 - bbox[0]
    oy = (size - h) // 2 - bbox[1]
    d.text((ox, oy), ch, font=font, fill=0)
    px = img.load()
    row_bytes = (size + 7) // 8
    out = bytearray(row_bytes * size)
    for y in range(size):
        for x in range(size):
            if px[x, y] >= threshold:  # 白 → 1
                out[y * row_bytes + x // 8] |= 0x80 >> (x % 8)
    return bytes(out)


def iter_gb2312():
    for qu in range(94):
        for wei in range(94):
            yield 0xA1 + qu, 0xA1 + wei


def parse_subset(path: str):
    seen, codes = set(), []
    with open(path, encoding="utf-8") as f:
        text = f.read()
    for ch in text:
        if ch in seen:
            continue
        seen.add(ch)
        try:
            raw = ch.encode("gb2312")
        except UnicodeEncodeError:
            continue
        if len(raw) == 2:
            codes.append((raw[0], raw[1]))
    return codes


def emit(name: str, size: int, entries: list, missing: int) -> str:
    """entries: [(unicode_codepoint, glyph_bytes)]，按 codepoint 排序后输出。

    输出按 Unicode 码点索引（二分查找），设备端直接 UTF-8 解析后查字，无需 GB2312 转换表。
    """
    entries.sort(key=lambda e: e[0])
    bpc = (size + 7) // 8 * size
    lines = [
        f"// 由 font_gen_ttf.py 生成（MiSans 派生点阵，{size}x{size}，{len(entries)} 字，缺 {missing}），勿手改",
        "#pragma once",
        "#include <Arduino.h>",
        "",
        f"#define FONT_{name.upper()}_W {size}",
        f"#define FONT_{name.upper()}_H {size}",
        f"#define FONT_{name.upper()}_BYTES_PER_CHAR {bpc}",
        f"#define FONT_{name.upper()}_CHARS {len(entries)}",
        "",
        "// 按 Unicode 码点升序；data 与之一一对应",
        f"static const uint32_t font_{name}_unicode[] PROGMEM = {{",
    ]
    row = []
    for cp, _ in entries:
        row.append(f"0x{cp:04X}")
        if len(row) == 8:
            lines.append("  " + ", ".join(row) + ",")
            row = []
    if row:
        lines.append("  " + ", ".join(row) + ",")
    lines += [
        "};",
        "",
        f"// 二分查找：返回字形在 font_{name}_data 中的字节偏移；-1 表示缺字",
        "static inline int16_t font_%s_lookup(uint32_t cp) {" % name,
        f"    int lo = 0, hi = {len(entries)} - 1;",
        "    while (lo <= hi) {",
        "        int mid = (lo + hi) >> 1;",
        f"        uint32_t v = pgm_read_dword(&font_{name}_unicode[mid]);",
        "        if (v == cp) return mid * %d;" % bpc,
        "        if (v < cp) lo = mid + 1; else hi = mid - 1;",
        "    }",
        "    return -1;",
        "}",
        "",
        f"extern const uint8_t font_{name}_data[];",
        "",
        f"const uint8_t font_{name}_data[] PROGMEM = {{",
    ]
    for cp, g in entries:
        assert len(g) == bpc
        lines.append(f"  /* U+{cp:04X} */ {','.join('0x%02X' % x for x in g)},")
    lines.append("};")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ttf", required=True)
    ap.add_argument("--size", type=int, choices=[16, 24], default=16)
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--subset")
    ap.add_argument("--threshold", type=int, default=128)
    args = ap.parse_args()

    font = ImageFont.truetype(args.ttf, args.size)
    codes = parse_subset(args.subset) if args.subset else list(iter_gb2312())
    entries, missing = [], 0
    for b1, b2 in codes:
        try:
            ch = bytes([b1, b2]).decode("gb2312")
        except UnicodeDecodeError:
            continue
        g = glyph_bitmap(font, ch, args.size, args.threshold)
        if g is None:
            missing += 1
            continue
        entries.append((ord(ch), g))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(emit(args.name, args.size, entries, missing))
    print(f"[ok] {args.out}: {len(entries)} 字, 缺 {missing}, {os.path.getsize(args.out)}B")


if __name__ == "__main__":
    main()
