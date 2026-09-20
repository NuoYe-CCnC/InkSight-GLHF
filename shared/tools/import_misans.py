#!/usr/bin/env python3
"""在本机校验 MiSans，并生成不进入开源包的固件字库。

字体文件只会复制到 ``.local/fonts/misans``；生成头文件写入固件源码目录，
但应由 ``.gitignore`` 排除。脚本不会联网，也不会打印字体内容。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fontTools.ttLib import TTFont, TTLibError


MAX_FONT_BYTES = 32 * 1024 * 1024
FONT_MAGIC = {b"OTTO", b"\x00\x01\x00\x00", b"true"}


class FontImportError(ValueError):
    pass


def _name(font: TTFont, name_id: int) -> str:
    table = font["name"]
    for record in table.names:
        if record.nameID == name_id:
            try:
                value = record.toUnicode().strip()
            except Exception:
                continue
            if value:
                return value
    return ""


def validate_font(path: Path, role: str, required_chars: str) -> dict[str, object]:
    path = path.expanduser()
    if path.is_symlink():
        raise FontImportError(f"{role}字体不能使用符号链接。")
    if not path.is_file():
        raise FontImportError(f"找不到{role}字体文件：{path}")
    if path.suffix.lower() not in {".otf", ".ttf"}:
        raise FontImportError(f"{role}字体必须是 OTF 或 TTF 文件。")
    size = path.stat().st_size
    if size <= 0 or size > MAX_FONT_BYTES:
        raise FontImportError(f"{role}字体大小必须在 1 字节到 32 MiB 之间。")
    with path.open("rb") as handle:
        magic = handle.read(4)
    if magic == b"ttcf":
        raise FontImportError(f"{role}字体不能使用 TTC 字体集合，请选择单独的 MiSans OTF。")
    if magic not in FONT_MAGIC:
        raise FontImportError(f"{role}字体文件头无效，不是受支持的 OTF/TTF。")
    try:
        font = TTFont(path, fontNumber=0, lazy=False)
    except (TTLibError, OSError) as exc:
        raise FontImportError(f"{role}字体无法解析：{exc}") from exc
    try:
        family = _name(font, 1)
        subfamily = _name(font, 2)
        version = _name(font, 5)
        if "misans" not in family.lower().replace(" ", ""):
            raise FontImportError(f"{role}字体家族不是 MiSans：{family or '未识别'}")
        style = subfamily.lower()
        if role == "常规" and "bold" in style:
            raise FontImportError("常规字体位置不能选择 Bold 字重。")
        if role == "粗体" and "bold" not in style:
            raise FontImportError(f"粗体字体未识别为 Bold：{subfamily or '未识别'}")
        cmap: set[int] = set()
        for table in font["cmap"].tables:
            cmap.update(table.cmap)
        missing = sorted({ord(ch) for ch in required_chars if not ch.isspace()} - cmap)
        if missing:
            sample = " ".join(f"U+{code:04X}" for code in missing[:12])
            raise FontImportError(f"{role}字体缺少面板所需字形（共 {len(missing)} 个）：{sample}")
    finally:
        font.close()
    return {
        "family": family,
        "subfamily": subfamily,
        "version": version or "未提供",
        "size_bytes": size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_name": path.name,
    }


def _run(command: list[str]) -> None:
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        message = (result.stderr or result.stdout or "未知错误").strip().splitlines()[-1]
        raise FontImportError(f"生成字库失败：{message}")


def generate(project_root: Path, regular: Path, bold: Path, destination: Path) -> None:
    tools_dir = project_root / "shared" / "tools"
    charset = project_root / "shared" / "firmware" / "fonts_full_charset.txt"
    if not charset.is_file():
        raise FontImportError("缺少字体字符清单 shared/firmware/fonts_full_charset.txt。")
    with tempfile.TemporaryDirectory(prefix="inksight-misans-") as temp_name:
        temp = Path(temp_name)
        ascii_subset = temp / "ascii.txt"
        ascii_subset.write_text("".join(chr(code) for code in range(32, 127)), encoding="utf-8")
        generated = temp / "generated"
        (generated / "fonts_gen").mkdir(parents=True)
        for role, font_path, sizes in (
            ("reg", regular, (16, 21, 24, 30, 62, 82)),
            ("bold", bold, (24, 30, 62, 82)),
        ):
            for size in sizes:
                subset = ascii_subset if size >= 62 else charset
                output = generated / "fonts_gen" / f"misans_{role}_{size}.h"
                _run([
                    sys.executable, str(tools_dir / "misans_raster.py"),
                    "--ttf", str(font_path), "--size", str(size),
                    "--subset", str(subset), "--name", f"misans_{role}_{size}",
                    "--out", str(output),
                ])
        for font_path, size, name, output_name in (
            (regular, 16, "misans_16", "fonts_misans_16.h"),
            (bold, 24, "misans_24", "fonts_misans_24.h"),
        ):
            _run([
                sys.executable, str(tools_dir / "font_gen_ttf.py"),
                "--ttf", str(font_path), "--size", str(size),
                "--subset", str(charset), "--name", name,
                "--out", str(generated / output_name),
            ])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "fonts_gen").mkdir(parents=True, exist_ok=True)
        for source in generated.rglob("*.h"):
            target = destination / source.relative_to(generated)
            os.replace(source, target)
            target.chmod(0o600)


def import_fonts(project_root: Path, regular: Path, bold: Path, *, check_only: bool = False) -> dict:
    charset_path = project_root / "shared" / "firmware" / "fonts_full_charset.txt"
    if not charset_path.is_file():
        raise FontImportError("缺少字体字符清单，无法验证面板字形覆盖。")
    required_chars = charset_path.read_text(encoding="utf-8")
    metadata = {
        "regular": validate_font(regular, "常规", required_chars),
        "bold": validate_font(bold, "粗体", required_chars),
    }
    if check_only:
        return metadata
    private_dir = project_root / ".local" / "fonts" / "misans"
    private_dir.mkdir(parents=True, exist_ok=True)
    private_dir.chmod(0o700)
    stored_regular = private_dir / "MiSans-Regular.otf"
    stored_bold = private_dir / "MiSans-Bold.otf"
    with tempfile.TemporaryDirectory(prefix="inksight-misans-copy-", dir=private_dir) as temp_name:
        staged_regular = Path(temp_name) / stored_regular.name
        staged_bold = Path(temp_name) / stored_bold.name
        shutil.copyfile(regular, staged_regular)
        shutil.copyfile(bold, staged_bold)
        staged_regular.chmod(0o600)
        staged_bold.chmod(0o600)
        generate(project_root, staged_regular, staged_bold, project_root / "shared" / "firmware" / "src")
        os.replace(staged_regular, stored_regular)
        os.replace(staged_bold, stored_bold)
    manifest = private_dir / "manifest.json"
    manifest.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest.chmod(0o600)
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description="本机校验并导入 MiSans 字体")
    parser.add_argument("--regular", required=True, type=Path, help="MiSans Regular OTF/TTF")
    parser.add_argument("--bold", required=True, type=Path, help="MiSans Bold OTF/TTF")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--check-only", action="store_true", help="只校验，不复制或生成")
    args = parser.parse_args()
    try:
        metadata = import_fonts(args.project_root.resolve(), args.regular, args.bold,
                                check_only=args.check_only)
    except FontImportError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    action = "校验通过" if args.check_only else "导入并生成完成"
    print(f"[完成] {action}。")
    for role in ("regular", "bold"):
        item = metadata[role]
        print(f"- {role}: {item['family']} {item['subfamily']} / {item['version']} / {item['size_bytes']} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
