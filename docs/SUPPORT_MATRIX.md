# Support matrix / 支持矩阵

This is the `v0.1.0-test.4` support boundary, not a roadmap promise. / 本表是 `v0.1.0-test.4` 的支持边界，不是路线图承诺。

| Area / 项目 | Verified / 已验证 | User-confirmed / 用户确认 | Not qualified / 未完成同级验收 |
|---|---|---|---|
| Host / 主机 | Standalone App on Apple Silicon macOS 26.5.2 with bundled CPython 3.11.15 and hash-locked wheels; source backend on macOS ARM64 Python 3.9 / 独立 App 与源码两条路径 | Local web console and current panel presentation / 本机管理端与当前面板呈现 | Other macOS versions, Windows, Linux, Intel Mac; clean-machine PlatformIO CLI bootstrap / 其他 macOS 版本及平台、全新电脑 PlatformIO CLI 引导 |
| MCU / 控制器 | ESP32-S3-DevKitC-1-N32R16V, 32 MB Octal flash, 16 MB Octal PSRAM | Current physical unit uses this path / 当前实机采用该路径 | Other boards listed in `platformio.ini` / 其他环境 |
| Display / 屏幕 | Waveshare 4.26-inch monochrome SSD1677, 800×480; candidate cloud payload upload/readback / 候选云端页面上传回读 | Operator observed AI usage reset expiries and Today full token/news display without overlap or repeated switching during the bounded window / 用户观察到两页关键数据、无重叠和反复切页 | Color panels, other resolutions, alternative controllers; no new firmware flash for this release / 彩色屏、其他分辨率、其他控制器；本版未重新刷写固件 |
| Backend / 后端 | App isolated first-run, lifecycle/port tests, bounded real WebDAV handoff and single-publisher restoration; Python 3.9 source lock and automated regression / App 隔离启动、进程/端口、真实云端短时接管及单发布者恢复，源码测试 | Existing physical setup and candidate page refresh / 既有实机与候选页面刷新 | Second clean Mac, Developer ID signing/notarization, long-running soak, production HA / 第二台全新 Mac、苹果公证、长期运行与高可用 |
| Local messages / 本地寄语 | Four exact GPL-3.0-only release messages, glyph/layout/rotation tests | Text and licensing choice / 文案与许可选择 | Large corpus or annual non-repeat / 大语料或全年不重复 |
| Remote news / 远程新闻 | Disabled by default; source/key gates and offline tests | — | No source is promised as legally or operationally ready by default / 不承诺任何来源默认可用 |
| Gold / 金价 | XAUS international spot conversion; timestamp/stale checks, bounded host/device wake retries and Beijing-day baseline tests | Current display observed by user / 用户见过当前显示；新补拉规则尚需长期运行观察 | Not domestic gold or prior close; provider uptime and off-market freshness are not guaranteed / 非国内金价或前收盘，不保证服务可用性及休市期间报价新鲜 |
| OpenAI costs | Organization Costs API integration and UTC-month semantics in tests | One observed `0.00 USD` display | Continuous polling, month rollover, offline carry-forward and backfill on hardware / 持续轮询、跨月、离线沿用和补拉实机验收 |
| Sleep / 休眠 | Host-wake recovery and device sleep logic covered by code/tests | — | Host cannot publish while asleep; every missed slot is not guaranteed to replay / 主机休眠不能出刊，不保证补全所有错过期次 |
| Battery / 电池 | No release-quality runtime measurement | — | No claim that 2000 mAh lasts one week / 不宣称 2000mAh 一周续航 |
| Firmware flash | Controlled fresh/update flow implemented for the supported target; new rollback path covered by offline simulation and read-only checks / 已有更新路径；新回退流程只有模拟与只读检查 | Prior controlled device work exists / 已有受控实机工作 | No new physical write/rollback was performed; the rollback button is not yet a qualified rescue feature / 本版未实机写入或回退，按钮不是已验收的救援功能 |

Automated tests and prior user confirmation are intentionally listed separately. A passing source test is not a substitute for a new-device hardware acceptance run. / 自动测试与用户确认刻意分栏；源码测试通过不能替代新设备实机验收。
