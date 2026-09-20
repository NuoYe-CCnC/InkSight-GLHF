# InkSight-GLHF first test release notes (archived draft)

> This draft is superseded by the final [`v0.1.0-test.1` test release notes](releases/v0.1.0-test.1.en.md). Backend API `1.1.0` is not the project release version.

## Summary

This is the first public test candidate for InkSight-GLHF, targeting ESP32-S3 N32R16V and the Waveshare 4.26-inch SSD1677 800×480 monochrome display. It includes the local backend, web console, `AI Usage` and `Today` panels, configuration tools, and firmware source.

## Included

- Local root administration, draft/validate/apply configuration, and two-page previews.
- Codex window information, DeepSeek balance/project token usage, and optional OpenAI organization UTC-month costs.
- XAUS international spot-gold conversion with a Beijing-day first-valid-quote baseline.
- All remote news sources disabled on fresh installs; four GPL-3.0-only local messages when no key/source is active.
- Local MiSans validation/import/glyph generation tools; no fonts or derived glyph data are distributed.
- Build and controlled fresh/update flash code for the single verified hardware target.
- GPL-3.0-only top-level license, third-party notices, SBOM, bilingual docs, security policy, and contribution files.

## Verified

- Hash-locked dependency installation and isolated import on macOS ARM64 with Python 3.9.
- At the 2026-09-14 baseline, 862 automated tests passed with two existing dependency warnings; 45 isolated candidate-ZIP tests passed.
- Candidate privacy, unsafe-path, font/binary exclusion, exact four-message, and reproducible-build checks.
- Verified firmware target: `epd_426_ssd1677_s3_n32r16`.

## User-confirmed

- Exact text and GPL-3.0-only treatment of the four release messages.
- The current final direction of the `AI Usage` page and the full-width/in-between-dividers information direction of the `Today` page.
- The device once displayed `API monthly spend 0.00 USD`; that observation does not validate continuous polling, month rollover, offline carry-forward, or backfill.

## Not completed or promised

- This work prepares docs and a candidate only. It does not reflash hardware, restart production services, or call paid APIs.
- Windows, Linux, Intel Mac, and other boards/displays do not have equivalent end-to-end qualification.
- A clean-machine PlatformIO CLI/platform bootstrap is not covered by a cross-platform hash lock.
- Scheduled publication cannot be guaranteed while macOS sleeps; wake recovery does not guarantee replay of every missed issue.
- A one-week lifetime from a 2000 mAh battery has not been measured and is not claimed.
- Remote news defaults to disabled. Deployers must review licensing, attribution, links, privacy, and service terms before enabling each source.
- The four local messages repeat; a large corpus or year-long non-repetition is not promised.

## Upgrade note

Updating source does not silently replace existing configuration. Back up private settings, the database, and runtime state, and inspect legacy migration with `migrate --dry-run`. Existing devices may use controlled `update` only with an exactly matching partition table; `fresh` accepts only genuinely blank critical regions. See the [installation and recovery guide](INSTALLATION.en.md).

## Release assets

- Source ZIP: `InkSight-Source.zip`
- SHA-256: use `InkSight-Source.zip.sha256` from the same Release
- GitHub repository: `https://github.com/NuoYe-CCnC/InkSight-GLHF`
- Tag: `v0.1.0-test.1`

Use the final test release notes and GitHub Release page as the source of truth.
