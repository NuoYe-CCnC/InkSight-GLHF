# InkSight-GLHF installation, configuration, build, and recovery

[中文](INSTALLATION.zh-CN.md) · [Back to README](../README.en.md)

This guide covers only the first test release's verified path: macOS ARM64, Python 3.9, ESP32-S3 N32R16V, and a 4.26-inch SSD1677 800×480 display. Other PlatformIO environments in the tree are not supported-hardware claims.

## 1. Requirements

- A macOS ARM64 host with Python 3.9.
- ESP32-S3-DevKitC-1-N32R16V: 32 MB Octal flash and 16 MB Octal PSRAM.
- Waveshare 4.26-inch monochrome SSD1677 800×480 e-paper.
- A data-capable USB cable and short jumper wires.
- PlatformIO Core for firmware builds. `espressif32@7.1.0` is pinned in the project, but the PlatformIO CLI itself does not yet have a cross-platform hash lock here.

The archive contains no production databases, state, caches, Wi-Fi credentials, API keys, accounts, real MAC addresses, fonts, or firmware binaries.

## 2. Backend

From the extracted release root:

```bash
python3.9 -m venv shared/backend/.venv
shared/backend/.venv/bin/python -m pip install --require-hashes \
  -r requirements-py39-macos-arm64.lock
shared/backend/.venv/bin/python shared/tools/inksight_config.py init
shared/backend/run-backend.sh
```

`init` creates a blank pair only when neither new configuration file exists. If legacy `manual_settings.json` is present, inspect before applying:

```bash
shared/backend/.venv/bin/python shared/tools/inksight_config.py migrate --dry-run
shared/backend/.venv/bin/python shared/tools/inksight_config.py migrate \
  --apply --confirm-activate
```

Open `http://127.0.0.1:8080/`. Create the first root account locally; there are no default credentials and the password must be at least 12 characters.

For closer browser previews, optionally fetch the OFL Noto Serif SC, Inter, and Lora fonts:

```bash
shared/backend/.venv/bin/python shared/backend/scripts/setup_fonts.py
```

These preview fonts are separate from the firmware's MiSans import.

## 3. Configuration and optional APIs

Public settings live in `shared/config/inksight_config.json`; secrets live in `shared/config/inksight_secrets.json` with mode `0600`; runtime state belongs under `shared/backend/state/` and must not be committed. The web console applies a draft only after validation and explicit confirmation.

```bash
shared/backend/.venv/bin/python shared/tools/inksight_config.py validate
shared/backend/.venv/bin/python shared/tools/inksight_config.py show-effective
```

All remote news sources default to off. Without an explicitly enabled, reviewed source and a news key, only the four local messages rotate; repetition is expected.

- DeepSeek daily tokens combine project response usage with a balance-change estimate and display the higher value. This is not an official whole-account total.
- OpenAI `API monthly spend` is the selected organization's UTC-calendar-month cost, not a balance or total personal Codex spend. It requires an Organization Owner Admin API Key.
- The Admin Key remains in local backend secrets and never enters firmware or a public payload. A normal API key, Codex login token, or backend `ADMIN_TOKEN` is not a substitute.

## 4. Import MiSans locally

Download MiSans yourself from [Xiaomi's official MiSans page](https://hyperos.mi.com/font/en/download/) and review its license. Then run:

```bash
shared/backend/.venv/bin/python shared/tools/import_misans.py \
  --regular /path/to/MiSans-Regular.otf \
  --bold /path/to/MiSans-Bold.otf \
  --check-only

shared/backend/.venv/bin/python shared/tools/import_misans.py \
  --regular /path/to/MiSans-Regular.otf \
  --bold /path/to/MiSans-Bold.otf
```

The second command keeps private copies under `.local/fonts/misans/` and creates ignored firmware glyph headers. Do not commit or redistribute the source fonts, generated headers, glyph caches, previews, or firmware binaries. Open-source software licensing does not grant redistribution rights for third-party fonts.

## 5. Wiring

| Display | ESP32-S3 |
|---|---|
| VCC | 3V3 |
| GND | GND |
| DIN | GPIO11 |
| CLK | GPIO12 |
| CS | GPIO10 |
| DC | GPIO13 |
| RST | GPIO14 |
| BUSY | GPIO4 |
| PWR (9-pin HAT) | GPIO5 |

Verify 3.3 V, common ground, BUSY, and PWR before power-on. Other boards and color 4.26-inch panels are not validated substitutes.

## 6. PlatformIO build

```bash
python3.9 -m venv .pio-venv
.pio-venv/bin/python -m pip install platformio
export PLATFORMIO_CORE_DIR="$PWD/.pio-core"
export INK_SSID="" INK_PASS="" INK_EXTRA_WIFI="" INK_SERVER=""
export CLOUD_BASE_URL="" CLOUD_USER="" CLOUD_PASS=""
export INKSIGHT_BUILD_ID="local-test"
.pio-venv/bin/python -m platformio run -d shared/firmware \
  -e epd_426_ssd1677_s3_n32r16
```

A clean computer downloads the pinned platform and libraries on first use; no private cache is assumed. Prefer the web console's private configuration for real Wi-Fi/cloud credentials so secrets do not end up in documentation, issues, or shell history.

## 7. Flash and upgrade

Prefer the local web console's firmware workflow. It fixes the target, snapshots configuration, identifies an ESP32-S3 with 32 MB flash again before writing, and requires a one-time confirmation.

- `fresh` accepts only a genuinely blank board and writes the bootloader, partition table, OTA initializer, and app0 without a whole-chip erase.
- `update` requires an exactly matching partition table, backs up NVS/OTA/the target app region, writes the inactive OTA app, verifies it, and only then changes the boot selection.

USB writes are high risk. Do not guess a port or erase an unknown board. Developers who understand the difference from the controlled flow can use direct PlatformIO upload:

```bash
.pio-venv/bin/python -m platformio run -d shared/firmware \
  -e epd_426_ssd1677_s3_n32r16 -t upload \
  --upload-port /dev/cu.YOUR_DEVICE
```

Direct upload does not provide the web workflow's blank-board check, backups, or matching-heartbeat confirmation.

## 8. Runtime, sleep, and recovery

- Run the backend in the foreground with `shared/backend/run-backend.sh`; stop it with `Ctrl-C`.
- A sleeping Mac cannot run the backend on schedule. Wake recovery handles only the currently eligible catch-up; it cannot replay every missed publication.
- ESP32 timed wake/deep sleep is independent. A device wake does not mean the host is awake.
- A one-week lifetime on a 2000 mAh battery has not been measured to a release-quality protocol and is not promised.

For local root password recovery:

```bash
shared/backend/.venv/bin/python shared/tools/admin_recovery.py
```

Back up private configuration, the database, and `shared/backend/state/` before upgrades, but never commit those backups. Configuration transactions and local backups reduce risk; they do not replace the user's own offline backup.

## 9. Troubleshooting

- `--` means unknown, unconfigured, or incomplete—not zero.
- No news is the safe default. Network news requires an enabled and authorized source.
- Missing firmware glyphs mean MiSans import has not completed; do not copy generated headers from someone else's archive.
- Missed publications after host sleep require an awake host or a separately reviewed always-on deployment.
- If flashing is rejected, do not bypass chip, capacity, partition, or blank-state checks.
