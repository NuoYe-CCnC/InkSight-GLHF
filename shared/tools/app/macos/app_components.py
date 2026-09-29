#!/usr/bin/env python3
"""Generate or verify the redistributable app's bundled Python component list."""
from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
from pathlib import Path
import sys


def inventory() -> dict:
    packages = []
    for distribution in metadata.distributions():
        info = distribution.metadata
        root = Path(distribution._path)
        notices = sorted(str(path.relative_to(root)) for path in root.rglob("*")
                         if path.is_file() and path.name.lower().startswith(
                             ("license", "licence", "copying", "notice", "copyright")))
        expression = info.get("License-Expression") or info.get("License") or "not in metadata"
        expression = expression.strip().splitlines()[0][:120]
        packages.append({"name": info.get("Name", root.name), "version": distribution.version,
                         "license_metadata": expression, "notice_files_in_dist_info": notices})
    packages.sort(key=lambda item: item["name"].casefold())
    return {"schema": 1, "runtime": "CPython " + sys.version.split()[0],
            "runtime_license_in_app": "Contents/Resources/Python/lib/python3.11/LICENSE.txt",
            "note": "Generated from the bundled interpreter; license metadata is not legal certification. Wheel metadata and available notice files remain in the app.",
            "packages": packages}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    result = inventory()
    if args.verify:
        if json.loads(args.output.read_text(encoding="utf-8")) != result:
            parser.error("bundled Python components differ from APP_COMPONENTS.json")
    else:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    print(f"bundled Python packages: {len(result['packages'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
