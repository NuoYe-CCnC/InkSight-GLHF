#!/usr/bin/env python3
"""Generate a local dependency snapshot, lock file, notices, and cached license texts."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import re
import shutil
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement


PY_LICENSES = {
    "annotated-doc": "MIT", "annotated-types": "MIT", "attrs": "MIT", "cffi": "MIT",
    "click": "BSD-3-Clause", "exceptiongroup": "MIT", "httpcore": "BSD-3-Clause",
    "idna": "BSD-3-Clause", "iniconfig": "MIT", "markdown-it-py": "MIT", "mdurl": "MIT",
    "packaging": "Apache-2.0 OR BSD-2-Clause", "pygments": "BSD-2-Clause",
    "starlette": "BSD-3-Clause", "tomli": "MIT", "typer": "MIT",
    "typing-extensions": "PSF-2.0", "urllib3": "MIT", "wrapt": "BSD-2-Clause",
    "fastapi": "MIT", "pydantic": "MIT", "uvicorn": "BSD-3-Clause", "pillow": "HPND",
    "httpx": "BSD-3-Clause", "openai": "Apache-2.0", "tenacity": "Apache-2.0",
    "python-dotenv": "BSD-3-Clause", "aiosqlite": "MIT", "apscheduler": "MIT",
    "slowapi": "MIT", "pyjwt": "MIT", "zhdate": "GPL-3.0-or-later", "lunar-python": "MIT",
    "qrcode": "BSD-3-Clause",
    "dashscope": "Apache-2.0", "cryptography": "Apache-2.0 OR BSD-3-Clause",
    "websockets": "BSD-3-Clause", "opuslib": "BSD-3-Clause", "phonenumbers": "Apache-2.0",
    "pyserial": "BSD-3-Clause", "fonttools": "MIT", "alibabacloud-alimt20181012": "Apache-2.0",
    "alibabacloud-tea-openapi": "Apache-2.0", "pytest": "MIT", "pytest-asyncio": "MIT",
}
FIRMWARE = [
    ("GxEPD2", "1.6.9", "GPL-3.0-only", True, "https://github.com/ZinggJM/GxEPD2/tree/1.6.9"),
    ("Adafruit GFX Library", "1.12.6", "BSD-3-Clause", True, "https://github.com/adafruit/Adafruit-GFX-Library/tree/1.12.6"),
    ("Adafruit BusIO", "1.17.4", "MIT", True, "https://github.com/adafruit/Adafruit_BusIO/tree/1.17.4"),
    ("WebSockets", "2.7.3", "LGPL-2.1-or-later", True, "https://github.com/Links2004/arduinoWebSockets/tree/2.7.3"),
    ("ArduinoJson", "7.4.3", "MIT", True, "https://github.com/bblanchon/ArduinoJson/tree/v7.4.3"),
    ("NimBLE-Arduino", "2.5.1", "Apache-2.0", True, "https://github.com/h2zero/NimBLE-Arduino/tree/2.5.1"),
]


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements(path: Path) -> list[Requirement]:
    result = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            result.append(Requirement(line))
    return result


def python_components(requirements: Path) -> list[dict[str, object]]:
    direct = {_norm(item.name): item.name for item in _requirements(requirements)}
    stack = list(direct.values())
    seen: dict[str, dict[str, object]] = {}
    while stack:
        requested = stack.pop()
        key = _norm(requested)
        if key in seen:
            continue
        try:
            dist = metadata.distribution(requested)
        except metadata.PackageNotFoundError:
            if key == "pyserial":
                seen[key] = {"name": "pyserial", "version": "3.5", "spdx": "BSD-3-Clause",
                             "direct": True, "installed_for_audit": False, "source": "https://pypi.org/project/pyserial/3.5/"}
                continue
            seen[key] = {"name": requested, "version": None, "spdx": "NOASSERTION",
                         "direct": key in direct, "installed_for_audit": False, "source": None}
            continue
        name = dist.metadata.get("Name", requested)
        license_value = (PY_LICENSES.get(key) or dist.metadata.get("License-Expression")
                         or dist.metadata.get("License") or "NOASSERTION")
        if license_value in {"", "UNKNOWN"}:
            license_value = "NOASSERTION"
        urls = dist.metadata.get_all("Project-URL") or []
        seen[key] = {"name": name, "version": dist.version, "spdx": license_value,
                     "direct": key in direct, "installed_for_audit": True,
                     "source": urls[0].split(",", 1)[-1].strip() if urls else dist.metadata.get("Home-page")}
        for value in dist.requires or []:
            try:
                req = Requirement(value)
            except Exception:
                continue
            if req.marker and not req.marker.evaluate(default_environment()):
                continue
            stack.append(req.name)
    return sorted(seen.values(), key=lambda item: str(item["name"]).lower())


def _copy_license(source: Path, target: Path) -> dict[str, object]:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    data = target.read_bytes()
    return {"path": target.as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def generate(root: Path) -> dict[str, object]:
    req = root / "shared/backend/requirements.txt"
    py = python_components(req)
    lock_lines = [
        "# Audit snapshot generated from the local Python 3.9 environment.",
        "# Exact versions only; use requirements-py39-macos-arm64.lock for the verified hashed install.",
    ]
    for item in py:
        if item["version"]:
            lock_lines.append(f"{item['name']}=={item['version']}")
    (root / "requirements.lock.txt").write_text("\n".join(lock_lines) + "\n", encoding="utf-8")

    platform_root = Path.home() / ".platformio/platforms"
    platform_path = platform_root / "espressif32@7.1.0/platform.json"
    if not platform_path.is_file():
        platform_path = platform_root / "espressif32/platform.json"
    platform = json.loads(platform_path.read_text())
    framework = json.loads(Path.home().joinpath(".platformio/packages/framework-arduinoespressif32/package.json").read_text())
    components = [{**item, "ecosystem": "pypi", "use": "backend runtime/test",
                   "distributed_in_source_zip": False, "user_fetched": True} for item in py]
    components += [{"name": n, "version": v, "spdx": lic, "direct": direct,
                    "ecosystem": "platformio-library", "use": "firmware link dependency",
                    "distributed_in_source_zip": False, "user_fetched": True, "source": url}
                   for n, v, lic, direct, url in FIRMWARE]
    components += [
        {"name": "PlatformIO espressif32", "version": platform.get("version"), "spdx": "Apache-2.0",
         "direct": True, "ecosystem": "platformio-platform", "use": "build only",
         "distributed_in_source_zip": False, "user_fetched": True, "source": "https://github.com/platformio/platform-espressif32"},
        {"name": "Arduino-ESP32 framework", "version": framework.get("version"),
         "spdx": framework.get("license", "NOASSERTION"), "direct": False, "ecosystem": "platformio-package",
         "use": "firmware framework", "distributed_in_source_zip": False, "user_fetched": True,
         "source": "https://github.com/espressif/arduino-esp32"},
    ]
    sbom = {"schema": 2, "snapshot_kind": "local-installed dependency closure",
            "project_license": "GPL-3.0-only",
            "python": {"requirements": "shared/backend/requirements.txt", "hash_locked": True,
                       "verified_lock": "requirements-py39-macos-arm64.lock",
                       "verified_python": "3.9", "verified_platform": "macOS ARM64",
                       "universal_lock": False},
            "firmware": {"environment": "epd_426_ssd1677_s3_n32r16", "cache_reused": True,
                         "platformio_platform": "espressif32@7.1.0"},
            "content_assets": [{
                "name": "release daily messages",
                "path": "shared/backend/data/daily_messages.json",
                "license": "GPL-3.0-only",
                "entries": 4,
                "source_overlay": "docs/phase14-content-source-release-2026-09-14/daily-messages-replacement-draft.json",
                "provenance": "publisher statement: mainly AI-assisted project drafting; no separately introduced third-party content",
                "production_file_overridden_in_candidate": True,
                "old_cc0_corpus_distributed": False,
            }],
            "components": components}
    (root / "SBOM.json").write_text(json.dumps(sbom, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lib = root / "shared/firmware/.pio/libdeps/epd_426_ssd1677_s3_n32r16"
    license_sources = [
        (lib / "GxEPD2/LICENSE", root / "LICENSES/GxEPD2-1.6.9-GPL-3.0.txt"),
        (lib / "WebSockets/LICENSE", root / "LICENSES/WebSockets-2.7.3-LGPL-2.1.txt"),
        (lib / "WebSockets/src/libb64/LICENSE", root / "LICENSES/WebSockets-libb64-PUBLIC-DOMAIN.txt"),
        (lib / "Adafruit GFX Library/license.txt", root / "LICENSES/Adafruit-GFX-1.12.6-BSD-3-Clause.txt"),
        (lib / "Adafruit BusIO/LICENSE", root / "LICENSES/Adafruit-BusIO-1.17.4-MIT.txt"),
        (lib / "ArduinoJson/LICENSE.txt", root / "LICENSES/ArduinoJson-7.4.3-MIT.txt"),
        (lib / "NimBLE-Arduino/LICENSE", root / "LICENSES/NimBLE-Arduino-2.5.1-Apache-2.0.txt"),
        (Path.home() / ".platformio/platforms/espressif32/LICENSE", root / "LICENSES/PlatformIO-espressif32-Apache-2.0.txt"),
    ]
    copied = [_copy_license(source, target) for source, target in license_sources if source.is_file()]
    notices = """# Third-party notices

This file is an engineering inventory, not legal advice. Project-owned material is offered under `GPL-3.0-only`; see `LICENSE` and `LICENSE-SCOPE.md`. The source archive declares but does not vendor Python or PlatformIO packages. Exact local versions are in `SBOM.json`; the verified Python 3.9/macOS ARM64 install lock is `requirements-py39-macos-arm64.lock`; standard upstream texts available in the audit cache are under `LICENSES/`.

## Firmware dependencies

| Component | Version | SPDX conclusion | Notes |
|---|---:|---|---|
| GxEPD2 | 1.6.9 | GPL-3.0-only | Local tag license is GPL v3; no project-specific “or later” grant was found. Appendix sample text is not treated as a grant. |
| WebSockets | 2.7.3 | LGPL-2.1-or-later | Source headers explicitly allow 2.1 or later; bundled libb64 notice is retained separately. |
| Adafruit GFX | 1.12.6 | BSD-3-Clause | Required directly; includes separately-noticed font/sample material upstream. |
| Adafruit BusIO | 1.17.4 | MIT | Transitive through GFX, explicitly pinned for reproducibility. |
| ArduinoJson | 7.4.3 | MIT | Required directly. |
| NimBLE-Arduino | 2.5.1 | Apache-2.0 plus bundled notices | Required directly; upstream tree contains additional component notices. |
| Arduino-ESP32 | local package snapshot | LGPL-2.1-or-later plus component licenses | Framework fetched by PlatformIO; not included in the source ZIP. |

## Python date dependency

| Component | Version | SPDX conclusion | Notes |
|---|---:|---|---|
| lunar-python | 1.4.8 | MIT | Required for lunar calendar and solar-term calculations; pure Python and installable on the verified macOS ARM64 baseline. |

## Fonts and visual resources

MiSans raw fonts, generated headers, glyph data, previews and firmware binaries are excluded. Users fetch MiSans only from Xiaomi's official page and run the local importer. Noto Serif SC, Inter and Lora are fetched from Google Fonts and identified there as OFL-1.1; no font files are distributed. The historical WenQuanYi mirror has no authoritative license file, so the public setup script no longer downloads it and uses the OFL vector-font fallback. Existing icon PNG files are excluded because their provenance is unverified.

## Adapted and upstream source

The codebase identifies `datascale-ai/inksight` as its upstream, whose repository is MIT-licensed; that exact upstream notice is retained under `LICENSES/InkSight-upstream-MIT.txt`. At revision `ea73138e8f69b8632d39295afeab32c3560d92c4`, 124 candidate files have exact Git-blob matches, including `epd_wft.h`, `epd4in2_wft.h`, built-in modes and the robot bitmap. The current workspace has no Git metadata, so this file-level evidence does not establish the actual fork point or ownership of later additions.

`EPD_4in26.*` and `DEV_Config.*` retain Waveshare's MIT-style file headers. They were compared with Waveshare e-Paper revision `a794fbc39656b0f93938d1ffb3fdc77eaed9e9fc`; local files include ESP32/Arduino adaptation and timeout/configuration changes, so they are not represented as byte-identical upstream copies.

## Project daily messages and runtime sources

The source candidate distributes four publisher-approved, AI-assisted project daily messages under `GPL-3.0-only`. The release builder places them at `shared/backend/data/daily_messages.json`; it does not distribute the separate 12-entry production corpus labelled `CC0-1.0`. The publisher states that the four messages were mainly generated through this project's Codex/DeepSeek tasks and that no third-party content was separately introduced. This is a publisher statement, not independent proof of originality, non-infringement, or ownership of all rights.

Runtime use of third-party news feeds is governed separately. Release defaults disable every remote source; an operator must review current source terms before enabling one. See the phase 14 source policy and matrix.
"""
    (root / "THIRD_PARTY_NOTICES.md").write_text(notices, encoding="utf-8")
    return {"python_components": len(py), "components": len(components), "licenses": copied}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    args = parser.parse_args()
    result = generate(args.root.resolve())
    print(json.dumps({"python_components": result["python_components"], "components": result["components"],
                      "license_files": len(result["licenses"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
