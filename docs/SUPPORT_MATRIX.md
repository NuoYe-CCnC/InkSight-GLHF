# Support matrix / 支持矩阵

This is the first-test-candidate support boundary, not a roadmap promise. / 本表是首个测试候选的支持边界，不是路线图承诺。

| Area / 项目 | Verified / 已验证 | User-confirmed / 用户确认 | Not qualified / 未完成同级验收 |
|---|---|---|---|
| Host / 主机 | macOS ARM64, Python 3.9, hash-locked backend install | Local web console and current panel presentation / 本机管理端与当前面板呈现 | Windows, Linux, Intel Mac; clean-machine PlatformIO CLI bootstrap / Windows、Linux、Intel Mac、全新电脑 PlatformIO CLI 引导 |
| MCU / 控制器 | ESP32-S3-DevKitC-1-N32R16V, 32 MB Octal flash, 16 MB Octal PSRAM | Current physical unit uses this path / 当前实机采用该路径 | Other boards listed in `platformio.ini` / 其他环境 |
| Display / 屏幕 | Waveshare 4.26-inch monochrome SSD1677, 800×480 | `AI 用量` and `今日关注` layout direction / 两页布局方向 | Color panels, other resolutions, alternative controllers / 彩色屏、其他分辨率或控制器 |
| Backend / 后端 | Python 3.9 macOS ARM64 dependency lock; offline import; automated regression | First-root and local configuration workflow / 首位管理员与本机配置流程 | Long-running clean-host soak, production HA / 全新主机长期运行与高可用 |
| Local messages / 本地寄语 | Four exact GPL-3.0-only release messages, glyph/layout/rotation tests | Text and licensing choice / 文案与许可选择 | Large corpus or annual non-repeat / 大语料或全年不重复 |
| Remote news / 远程新闻 | Disabled by default; source/key gates and offline tests | — | No source is promised as legally or operationally ready by default / 不承诺任何来源默认可用 |
| Gold / 金价 | XAUS international spot conversion and Beijing-day baseline logic in tests | Current display observed by user / 用户见过当前显示 | It is not domestic gold or prior close; provider uptime is not guaranteed / 非国内金价或前收盘，不保证服务可用性 |
| OpenAI costs | Organization Costs API integration and UTC-month semantics in tests | One observed `0.00 USD` display | Continuous polling, month rollover, offline carry-forward and backfill on hardware / 持续轮询、跨月、离线沿用和补拉实机验收 |
| Sleep / 休眠 | Host-wake recovery and device sleep logic covered by code/tests | — | Host cannot publish while asleep; every missed slot is not guaranteed to replay / 主机休眠不能出刊，不保证补全所有错过期次 |
| Battery / 电池 | No release-quality runtime measurement | — | No claim that 2000 mAh lasts one week / 不宣称 2000mAh 一周续航 |
| Firmware flash | Controlled fresh/update flow implemented and previously validated for the supported target | Prior controlled device work exists / 已有受控实机工作 | No new physical reflash was performed for this documentation-only release prep / 本次仅文档准备，未重新烧录 |

Automated tests and prior user confirmation are intentionally listed separately. A passing source test is not a substitute for a new-device hardware acceptance run. / 自动测试与用户确认刻意分栏；源码测试通过不能替代新设备实机验收。
