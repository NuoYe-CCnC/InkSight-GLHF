#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""misans_raster.py — MiSans 度量型点阵生成（第二阶段）

与 font_gen_ttf.py（方框居中 16/24）不同：本工具按“笔位+基线”语义生成字形，
每个字形记录：
  unicode / advance / x0(左bearing) / inkTop(基线以上像素) / inkW / inkH / dataOffset
位约定与 imgBuf 一致：1=白 0=黑，每字节 MSB 为左侧像素（行列式，按 ink 裁剪）。

输出头文件布局（32 位偏移，防 int16 溢出）：
  font_<name>_W/H（=size 参考），font_<name>_count，
  const uint32 font_<name>_unicode[]，_adv[]，_x0[]，_top[]，_iw[]，_ih[]，_off[]，
  const uint8 font_<name>_data[]，查找函数（二分，int32 偏移）。
度量：以 baseline 为 y 原点（行内）。truetype size = args.size；
  ascent/descent 取 PIL getmetrics；每字 bbox 相对 (0, baseline=ascent)。
用法示例：
  python misans_raster.py --ttf MiSans-Regular.ttf --size 24 --subset ui_chars.txt \
      --name misans_reg_24 --out ../../firmware/src/font_misans_reg_24.h
缺字输出清单并报错计数（由调用方决定替换策略）。
"""
from __future__ import annotations

import argparse
import hashlib
import os

from PIL import Image, ImageDraw, ImageFont


def collect_chars(subset_files, extra=""):
    # 空格(U+0020)是必需字形：面板含 "7 日窗口"、"状态 INKSIGHT" 等串。
    # 仅丢弃换行/制表等排版空白，保留普通空格。
    def _keep(ch):
        return not (ch in "\n\r\t\f\v\u00a0")
    seen = []
    for p in subset_files:
        with open(p, encoding="utf-8") as f:
            text = f.read()
        for ch in text:
            if ch not in seen and _keep(ch):
                seen.append(ch)
    for ch in extra:
        if ch not in seen and _keep(ch):
            seen.append(ch)
    return seen


def raster_one(font, ascent: int, ch: str, threshold: int):
    """返回 dict 或 None（缺字）。度量以 baseline 为原点（PIL 默认锚 'la'：
    绘制点=ascender 线，baseline=绘制点 y + ascent；经实测确认）。"""
    try:
        advance = font.getlength(ch)
        bbox = font.getbbox(ch)   # 相对 ascender 锚点 (0,0)
    except Exception:  # noqa: BLE001
        return None
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:  # 空白
        return {"adv": int(round(advance * 64)), "x0": 0, "top": 0, "iw": 0, "ih": 0, "data": b""}
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    pad = 2
    img = Image.new("L", (w + pad * 2, ascent + descent_needed + pad * 2), 255)
    d = ImageDraw.Draw(img)
    # 绘制点 y=pad ⇒ ascender 在 pad，baseline 在 pad+ascent；
    # ink 区 = ascender_y + bbox[1] .. + bbox[3]-1（列同理由 bbox[0] 起）。
    d.text((pad, pad), ch, font=font, fill=0)
    px = img.load()
    row_bytes = (w + 7) // 8
    out = bytearray(row_bytes * h)
    for r in range(h):            # r=0 对应 ink 顶（画布行 pad+bbox[1]）
        yy = pad + bbox[1] + r
        for c in range(w):        # c=0 对应 ink 左（画布列 pad+bbox[0]）
            xx = pad + bbox[0] + c
            if 0 <= xx < img.width and 0 <= yy < img.height and px[xx, yy] >= threshold:
                out[r * row_bytes + c // 8] |= 0x80 >> (c % 8)
    return {
        "adv": int(round(advance * 64)),
        "x0": bbox[0],
        "top": ascent - bbox[1],  # baseline 到 ink 顶（正=在基线以上）
        "iw": w, "ih": h,
        "data": bytes(out),
    }


descent_needed = 8  # 兜底画布


def emit(name: str, size: int, entries, missing: list[str], source: str) -> str:
    lines = []
    lines.append(f"// {name} — MiSans 派生点阵（{source}），size={size}，{len(entries)} 字，缺 {len(missing)}")
    lines.append("// 生成：shared/tools/misans_raster.py；位约定 1=白 0=黑，MSB-first；勿手改")
    lines.append(f"#define FONT_{name.upper()}_COUNT {len(entries)}")
    lines.append(f"static const uint32_t font_{name}_unicode[] PROGMEM = {{")
    lines.append(",".join(f"0x{cp:04X}" for cp, _ in entries))
    lines.append("};")
    for fld, suffix, ctype in (("adv", "_adv", "uint16_t"), ("x0", "_x0", "int16_t"),
                                ("top", "_top", "int16_t"), ("iw", "_iw", "uint16_t"),
                                ("ih", "_ih", "uint16_t")):
        lines.append(f"static const {ctype} font_{name}{suffix}[] PROGMEM = {{")
        lines.append(",".join(str(e[fld]) for _, e in entries))
        lines.append("};")
    lines.append(f"static const uint32_t font_{name}_off[] PROGMEM = {{")
    off = 0
    offs = []
    for _, e in entries:
        offs.append(off)
        rb = (e["iw"] + 7) // 8
        off += rb * e["ih"]
    lines.append(",".join(str(o) for o in offs))
    lines.append("};")
    lines.append("// 二分查找：返回下标；-1 缺字")
    lines.append(f"static inline int32_t font_{name}_lookup(uint32_t cp) {{")
    lines.append("  int32_t lo=0, hi=" + str(len(entries) - 1) + ";")
    lines.append("  while (lo<=hi) { int32_t mid=(lo+hi)/2;")
    lines.append("    uint32_t v=pgm_read_dword(&font_%s_unicode[mid]);" % name)
    lines.append("    if (v==cp) return mid; if (v<cp) lo=mid+1; else hi=mid-1; }")
    lines.append("  return -1;")
    lines.append("}")
    lines.append("// 字形数据（每字行字节数 = (iw+7)/8 * ih，行内 1=白）")
    lines.append(f"static const uint8_t font_{name}_data[] PROGMEM = {{")
    lines.append(",".join(str(v) for _, e in entries for v in e["data"]))
    lines.append("};")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ttf", required=True)
    ap.add_argument("--size", type=int, required=True)
    ap.add_argument("--subset", action="append", default=[])
    ap.add_argument("--extra", default="")
    ap.add_argument("--threshold", type=int, default=128)
    ap.add_argument("--name", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    chars = sorted(collect_chars(args.subset, args.extra),
                   key=lambda c: ord(c))
    font = ImageFont.truetype(args.ttf, args.size)
    # cmap 成员检测（若可用）：缺字报错而不是输出 notdef 方块
    cmap = None
    try:
        from fontTools.ttLib import TTFont
        ft = TTFont(args.ttf, fontNumber=0)
        cmap = set()
        for table in ft["cmap"].tables:
            cmap.update(table.cmap.keys())
    except Exception:
        cmap = None
    metrics = font.getmetrics()
    global descent_needed
    descent_needed = max(8, metrics[1])
    entries = []
    missing = []
    for ch in chars:
        cp = ord(ch)
        if cmap is not None and cp not in cmap:
            missing.append(ch)
            continue
        g = raster_one(font, metrics[0], ch, args.threshold)
        if g is None:
            missing.append(ch)
            continue
        entries.append((cp, g))
    entries.sort(key=lambda x: x[0])
    digest = hashlib.sha256(open(args.ttf, "rb").read()).hexdigest()[:16]
    header = emit(args.name, args.size, entries, missing,
                  f"ttf_sha={digest} {os.path.basename(args.ttf)}")
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(header)
    data_bytes = sum((e["iw"] + 7) // 8 * e["ih"] for _, e in entries)
    print(f"[ok] {args.out}: {len(entries)} 字, 缺 {len(missing)}, data={data_bytes}B")
    if missing:
        print("missing:", "".join(missing[:200]))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
