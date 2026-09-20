# InkSight-GLHF

[中文](README.md)

> A local two-page information panel for an ESP32-S3 and a 4.26-inch e-paper display, with AI usage, news/gold views, browser-based configuration, and reproducible firmware builds.

InkSight-GLHF is an open-source project preparing its first test release. It combines a local backend, a browser-based management console, and ESP32 firmware. It is intended for developers and hardware hobbyists who are comfortable wiring, configuring, and maintaining their own service. This project is not an official product of, or partnership with, OpenAI, DeepSeek, Xiaomi, or any hardware vendor.

## What it does

- `AI Usage`: shows available Codex window information, DeepSeek balance/project token usage, and optional OpenAI organization monthly costs.
- `Today`: shows either local messages or an enabled news digest, plus converted XAUS international spot-gold data.
- Local web console: create the first root administrator, edit a draft, validate it, explicitly apply it, and preview both panels.
- Firmware build and controlled flashing: an engineering path for one verified hardware target, with separate fresh-board and update flows.
- No paid API is required to start: with remote sources and keys absent, the news area uses four approved local messages.

Not every value is a real-time provider account total. In particular, `API monthly spend` means costs returned by the OpenAI Organization Costs API for the selected organization and the current UTC calendar month. It is not an API balance and not the user's total personal Codex spend.

## Current support

| Area | Current status |
|---|---|
| Controller | Verified: ESP32-S3-DevKitC-1-N32R16V / ESP32-S3-WROOM-2-N32R16V, 32 MB Octal flash, 16 MB Octal PSRAM |
| Display | Verified: Waveshare 4.26-inch monochrome e-paper, SSD1677, 800×480 |
| Firmware environment | `epd_426_ssd1677_s3_n32r16` |
| Host | Verified baseline: macOS ARM64 with Python 3.9 |
| Windows / Linux / Intel Mac | Source may run, but no equivalent end-to-end qualification has been completed for the first test release |
| Other boards or panels | Historical/experimental environments exist in the tree but are not supported targets for this test release |
| Battery life | Not measured to a release-quality protocol; no one-week claim is made for a 2000 mAh battery |

Hardware references: [Waveshare 4.26-inch product](https://www.waveshare.com/product/displays/e-paper/4.26inch-e-paper-hat.htm) · [Waveshare documentation](https://www.waveshare.com/wiki/4.26inch_e-Paper_HAT_Manual) · [Espressif ESP32-S3-DevKitC-1 guide](https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp32-s3-devkitc-1/user_guide_v1.0.html)

## Quick start

Run these commands from the extracted release root. The verified baseline uses Python 3.9:

```bash
python3.9 -m venv shared/backend/.venv
shared/backend/.venv/bin/python -m pip install --require-hashes \
  -r requirements-py39-macos-arm64.lock
shared/backend/.venv/bin/python shared/tools/inksight_config.py init
shared/backend/run-backend.sh
```

Open `http://127.0.0.1:8080/`. An empty database prompts for the first root account. There is no default username or password; the password must contain at least 12 characters.

Fresh-install defaults are deliberately conservative:

- All remote news sources are disabled; news is not fetched automatically out of the box.
- Without a configured news key and explicitly enabled source, the panel rotates only the four bundled local messages. Long-term non-repetition is not promised.
- `Credit balance` and `API monthly spend` switches default to off.
- Wi-Fi credentials, cloud accounts, API keys, real MAC addresses, membership dates, and production databases are not included.
- Configuration initialization does not overwrite an existing setup; inspect legacy migration with `migrate --dry-run` first.

Before building firmware, download MiSans from Xiaomi's official source and import it locally. The repository and release archive do not distribute MiSans, derived glyph headers, previews, or firmware binaries. See the [installation, build, and recovery guide](docs/INSTALLATION.en.md).

## The two panels

1. `AI Usage`: Codex windows/credits, DeepSeek balance and project token usage, and optional OpenAI organization monthly costs.
2. `Today`: news or local messages, plus converted international spot-gold data.

Page switching is controlled by `device_policy.page_switch`. Data can be cached, estimated, or manually supplied; unknown values remain `--` rather than being presented as zero. Remote news is generated only after the deployer reviews each source's terms, explicitly enables it, and configures the required key.

## Runtime and sleep limitations

`shared/backend/run-backend.sh` is the foreground entry point included in the public candidate. Local processes cannot run on schedule while the Mac host is asleep. Wake recovery can idempotently catch up only the currently eligible task; it does not guarantee replay of every missed publication time. ESP32 timed wake/deep sleep is a separate path: a device wake does not imply that the host backend is awake. Keep the host awake for reliable scheduled publication, or deploy to an independently reviewed always-on environment.

## Optional external services

- DeepSeek: balance lookup, news generation, and project token accounting; pricing and availability are controlled by the provider.
- OpenAI Organization Costs API: read only after the user opts in and supplies an Admin API Key. The key stays in private local backend configuration and is never placed in firmware or a public device payload.
- News sources: all default to disabled. Open-source software does not grant rights to republish, translate, or cache news content.
- Nutstore/WebDAV: an optional device-delivery route that users configure and review themselves.

## Documentation

- [Install, configure, import fonts, build, flash, upgrade, and recover](docs/INSTALLATION.en.md)
- [中文安装指南](docs/INSTALLATION.zh-CN.md)
- [First test release notes draft](docs/RELEASE_NOTES_DRAFT.en.md)
- [Support matrix and known limitations](docs/SUPPORT_MATRIX.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md) · [License scope](LICENSE-SCOPE.md) · [SBOM](SBOM.json)

## License

Project-owned code and the four release messages that the distributor is entitled to license are offered under `GPL-3.0-only`; see [LICENSE](LICENSE). Third-party code, fonts, services, and content remain under their own terms. GPL does not relicense third-party material and does not prove ownership of external rights. MiSans is not distributed.

These files represent a local engineering release candidate. They do not mean that a public repository, tag, or GitHub Release has already been created.
