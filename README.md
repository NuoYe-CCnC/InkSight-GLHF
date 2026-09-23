# InkSight-GLHF

[English](README.en.md)

> 面向 ESP32-S3 与 4.26 英寸墨水屏的本地双页信息面板，提供 AI 用量、新闻/金价展示、网页配置与可复现固件构建。

InkSight-GLHF 是一个公开测试阶段的开源项目。它由本机后端、浏览器管理端和 ESP32 固件组成，主要面向愿意自行接线、配置与维护服务的开发者和硬件爱好者。本项目不是 OpenAI、DeepSeek、小米或硬件厂商的官方产品或合作项目。

仓库：[`NuoYe-CCnC/InkSight-GLHF`](https://github.com/NuoYe-CCnC/InkSight-GLHF) · 当前测试版：[`v0.1.0-test.1`](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/tag/v0.1.0-test.1)

## 能做什么

- `AI 用量`页：显示可获得的 Codex 窗口信息、DeepSeek 余额/本项目调用 Token，以及可选的 OpenAI 组织月消费。
- `今日关注`页：显示本地寄语或启用后的新闻摘要，并显示 XAUS 国际现货金价换算数据。
- 本机网页管理端：首次创建 root 管理员后，编辑配置草稿、校验并确认应用，预览两套面板。
- 固件构建与受控刷写：只对已验证硬件目标提供工程路径，区分空白板安装和已有设备更新。
- 无付费 API 也可运行：远程新闻源和相关密钥缺省时，新闻区域使用四条已确认的本地寄语。

这里的“用量”和“消费”并不都代表供应商账户的实时总账。尤其是 `API 本月消费`，口径是所选 OpenAI 组织在 UTC 自然月内由 Organization Costs API 返回的消费，不是 API 余额，也不是个人 Codex 的全部开销。

## 当前支持范围

| 项目 | 当前结论 |
|---|---|
| 控制器 | 已验证：ESP32-S3-DevKitC-1-N32R16V / ESP32-S3-WROOM-2-N32R16V，32 MB Octal Flash、16 MB Octal PSRAM |
| 显示屏 | 已验证：微雪 4.26 英寸黑白墨水屏，SSD1677，800×480 |
| 固件环境 | `epd_426_ssd1677_s3_n32r16` |
| 主机 | 已验证基线：macOS ARM64、Python 3.9 |
| Windows / Linux / Intel Mac | 源码可能可运行，但没有完成同等级整套验收，不属于首个测试版支持承诺 |
| 其他开发板或屏幕 | 仓库中存在历史/实验环境，但不属于本测试版已支持硬件 |
| 电池续航 | 尚无规范测量；不宣称 2000 mAh 可运行一周 |

硬件参考：[微雪 4.26 英寸产品页](https://www.waveshare.com/product/displays/e-paper/4.26inch-e-paper-hat.htm) · [微雪官方说明](https://www.waveshare.com/wiki/4.26inch_e-Paper_HAT_Manual) · [乐鑫 ESP32-S3-DevKitC-1 说明](https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp32-s3-devkitc-1/user_guide_v1.0.html)

## 快速开始

可以克隆仓库或下载 Release 源码包。以下命令均从源码根目录执行；首个验证基线使用 Python 3.9：

```bash
git clone https://github.com/NuoYe-CCnC/InkSight-GLHF.git
cd InkSight-GLHF
```

```bash
python3.9 -m venv shared/backend/.venv
shared/backend/.venv/bin/python -m pip install --require-hashes \
  -r requirements-py39-macos-arm64.lock
shared/backend/.venv/bin/python shared/tools/inksight_config.py init
shared/backend/run-backend.sh
```

浏览器打开 `http://127.0.0.1:8080/`。空数据库会引导创建首位 root；项目没有默认用户名或默认密码，密码至少 12 位。

新安装的默认规则：

- 远程新闻源全部关闭；不会开箱自动抓取新闻。
- 没有新闻密钥或启用来源时，仅轮播四条本地寄语；不承诺长期不重复。
- `点数余额`、`API 本月消费`显示开关默认关闭。
- Wi-Fi、云端账号、API Key、真实 MAC、会员日期和生产数据库不会随公开包提供。
- 配置工具不会覆盖现有配置；旧配置先使用 `migrate --dry-run` 检查。

固件构建前必须自行从 MiSans 官方渠道下载字体并在本机导入。仓库和发行包不提供 MiSans 字体、派生字库、预览图或固件二进制。详见[安装、配置、构建与恢复指南](docs/INSTALLATION.zh-CN.md)。

## 两套面板

1. `AI 用量`：Codex 窗口/点数、DeepSeek 余额与本项目 Token、可选的 OpenAI 组织月消费。
2. `今日关注`：新闻或本地寄语，以及国际现货金价换算。

页面切换由 `device_policy.page_switch` 控制。页面数据可能来自缓存、估算或手动资料；未知值保持 `--`，不伪装成 `0`。远程新闻只有在部署者逐源审查条款、主动启用来源并配置所需密钥后才会生成。

计划任务自动生成新闻时，产生的 DeepSeek 扣费仍会更新真实余额与当日 Token，但不会仅因这笔后台扣费从“今日关注”切到“AI 用量”。手动发刊、其他 API 使用、充值以及 Codex 用量变化仍按页面切换规则处理。该归因有时间和金额上限；无法确认的网络请求只会短暂抑制，超时后会按普通活动处理。

## 运行与休眠边界

`shared/backend/run-backend.sh` 是公开候选中的前台运行入口。主机进入 macOS 睡眠后，本机进程不能保证按计划执行；唤醒后的幂等恢复只能补当前允许补跑的任务，不能保证每个错过时刻都重新发布。ESP32 的定时唤醒/深度睡眠是另一条独立链路，设备唤醒不等于主机后端已经运行。需要持续出刊时，应让后端所在主机保持唤醒，或自行部署一个经过审查的常在线环境。

## 可选外部服务

- DeepSeek：用于余额查询、新闻生成和本项目调用 Token 统计；费用和可用性由服务方决定。
- OpenAI Organization Costs API：仅在用户主动开启并配置 Admin API Key 后读取组织 UTC 月消费。Admin Key 只保存在本机后端私密配置中，不进入固件或公开设备载荷。
- 新闻源：新发行全部默认关闭；软件开源不代表新闻内容获准转载、翻译或缓存。
- 坚果云/WebDAV：是可选的设备数据传递方式，需要用户自行配置和审查服务条款。

## 文档

- [安装、配置、字体、构建、刷写、升级与恢复](docs/INSTALLATION.zh-CN.md)
- [English installation guide](docs/INSTALLATION.en.md)
- [v0.1.0-test.1 测试版说明](docs/releases/v0.1.0-test.1.zh-CN.md)
- [支持矩阵与已知限制](docs/SUPPORT_MATRIX.md)
- [贡献指南](CONTRIBUTING.md)
- [安全政策](SECURITY.md)
- [第三方通知](THIRD_PARTY_NOTICES.md) · [许可范围](LICENSE-SCOPE.md) · [SBOM](SBOM.json)

## 许可

项目有权许可的自有代码与四条发行寄语按 `GPL-3.0-only` 提供，完整文本见 [LICENSE](LICENSE)。第三方代码、字体、服务和内容继续受各自条款约束；GPL 不会重授第三方材料，也不证明发布者拥有所有外部权利。MiSans 不随发行包提供。

`v0.1.0-test.1` 是首个公开测试版，不是稳定版。已验证、用户确认与尚未验证的范围以[测试版说明](docs/releases/v0.1.0-test.1.zh-CN.md)和[支持矩阵](docs/SUPPORT_MATRIX.md)为准。
