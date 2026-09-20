# Third-party notices

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

The source candidate distributes four publisher-approved, AI-assisted project
daily messages under `GPL-3.0-only`. The release builder places them at
`shared/backend/data/daily_messages.json`; it does not distribute the separate
12-entry production corpus labelled `CC0-1.0`. The publisher states that the four
messages were mainly generated through this project's Codex/DeepSeek tasks and
that no third-party content was separately introduced. This is a publisher
statement, not independent proof of originality, non-infringement, or ownership
of all rights.

Runtime use of third-party news feeds is governed separately. Release defaults
disable every remote source; an operator must review current source terms before
enabling one. See the phase 14 source policy and matrix.
