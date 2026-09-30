# InkSight-GLHF

[中文](README.md) · [Documentation index](docs/README.md)

A two-page information panel for an ESP32-S3 and a 4.26-inch monochrome e-paper display. `AI Usage` shows available Codex windows, DeepSeek balance and this project's token use; `Today` shows local messages or user-enabled news and converted XAUS international spot-gold data. The project includes a local backend, web console and firmware source. It is not an official product of OpenAI, DeepSeek, Xiaomi or a hardware vendor.

> This public test release provides a self-contained `InkSight.app` verified on Apple Silicon with macOS 26.5.2, plus a separate source archive. The app is **not Developer ID signed or Apple notarized**, so macOS may show a security warning on first launch. There is still no ready-to-flash firmware binary. Physical display use additionally requires wiring, a locally obtained licensed MiSans font and a firmware build/flash. No privacy-approved real-screen photograph is available for public display yet.

## Start here

1. [Download test release `v0.1.0-test.5`](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/tag/v0.1.0-test.5) and read the [download and checksum guide](docs/DOWNLOAD.zh-CN.md). The Mac app and the source archive are different assets.
2. [Mac app first use](docs/APP_DESKTOP.zh-CN.md) (Chinese): the runtime is bundled. The [source setup](docs/FIRST_USE.zh-CN.md) remains available separately.
3. [Upgrade an existing setup](docs/UPGRADE.zh-CN.md): distinguish a backend-only update from a firmware flash and back up private state first.

For ongoing operation, use the [documentation index](docs/README.md). Contributors should start with [CONTRIBUTING.md](CONTRIBUTING.md); the detailed English source-install path remains in [INSTALLATION.en.md](docs/INSTALLATION.en.md).

## Qualified scope and defaults

| Area | Test-release boundary |
|---|---|
| Host | Standalone app: Apple Silicon, macOS 26.5.2, bundled Python 3.11.15. Source backend: macOS ARM64 and Python 3.9. Other macOS versions, Windows, Linux and Intel Mac have not received equivalent qualification. |
| Controller | ESP32-S3-DevKitC-1-N32R16V / ESP32-S3-WROOM-2-N32R16V, 32 MB Octal flash and 16 MB Octal PSRAM. |
| Display | Waveshare 4.26-inch monochrome SSD1677, 800×480: [product](https://www.waveshare.com/product/displays/e-paper/4.26inch-e-paper-hat.htm), [original manual](https://www.waveshare.com/wiki/4.26inch_e-Paper_HAT_Manual), [Espressif board guide](https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp32-s3-devkitc-1/user_guide_v1.0.html). |
| Firmware target | `epd_426_ssd1677_s3_n32r16` only. Other historical build environments are not qualified hardware. |
| Battery | A one-week runtime claim for `2000 mAh` has not been measured to a release-quality protocol. |

**All remote news sources are disabled** in a fresh install. Without an explicitly enabled source and key, the panel uses four local messages and does not call a paid model automatically. Optional OpenAI `API monthly spend` is the selected organization's cost for a **UTC calendar month**, not an API balance or all personal Codex spending. DeepSeek's daily token figure is not an official whole-account ledger. Unknown values remain `--`, not zero. See [configuration and data semantics](docs/CONFIGURATION.zh-CN.md) and the [support matrix](docs/SUPPORT_MATRIX.md).

The first local web-console visit creates a root account; **there is no default username or password**. Private configuration, API keys, real device identifiers and runtime data are not distributed. A sleeping Mac cannot run the backend scheduler, and an ESP32 wake does not wake the host. See [first use](docs/FIRST_USE.zh-CN.md) and [host publication](docs/HOST_RUNTIME.zh-CN.md).

## License and status

Project-owned code and the four releasable local messages are licensed under [`GPL-3.0-only`](LICENSE). Third-party code, fonts, news content and services keep their own terms. MiSans is not distributed in this repository or the release archive. See [license scope](LICENSE-SCOPE.md), [third-party notices](THIRD_PARTY_NOTICES.md) and [SBOM](SBOM.json).

The [`v0.1.0-test.5` notes](docs/releases/v0.1.0-test.5.en.md) document desktop collection, request queues, wake recovery and data migration; no new firmware binary is included. [Report security issues privately](SECURITY.md), never in a public Issue with credentials or device identifiers.
