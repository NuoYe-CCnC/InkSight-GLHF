# InkSight-GLHF

[English](README.en.md) · [文档目录](docs/README.md)

面向 ESP32-S3 与 4.26 英寸黑白墨水屏的双页信息面板：`AI 用量`显示可获得的 Codex 窗口、DeepSeek 余额与本项目 Token；`今日关注`显示本地寄语或经用户启用的新闻，以及 XAUS 国际现货金价换算。项目包含本机后端、网页管理端和固件源码，不是 OpenAI、DeepSeek、小米或硬件厂商的官方产品。

> 当前是公开测试版，**只提供源码包，不提供一键安装程序或可直接刷写的固件二进制**。下载 ZIP 后仍须安装依赖、配置服务；要使用实体屏，还须自行接线、合法取得 MiSans 并构建/刷写固件。没有已获公开许可的实屏照片，本页暂不放可能暴露私人余额或账号的图片。

## 从这里开始

1. [下载最新测试版 `v0.1.0-test.2`](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/tag/v0.1.0-test.2)——请先看[文件选择与校验](docs/DOWNLOAD.zh-CN.md)；GitHub 自动生成的 `Source code` 和项目提供的 `InkSight-Source.zip` 都不是安装器。
2. [首次使用](docs/FIRST_USE.zh-CN.md)——先在已验证的 Mac 上启动本地后端，再决定是否配置云端发布与实体屏。
3. [升级已有设备](docs/UPGRADE.zh-CN.md)——先分清仅更新后端与需要刷写固件的版本，备份后再操作。

只想了解运行中的配置和日常检查，可从[用户文档目录](docs/README.md)进入；贡献代码请走[开发者路径](CONTRIBUTING.md)。

## 已验证范围与默认行为

| 部分 | 本测试版的边界 |
|---|---|
| 主机 | macOS ARM64、Python 3.9；Windows、Linux、Intel Mac 尚未完成同等级验收 |
| 控制器 | ESP32-S3-DevKitC-1-N32R16V / ESP32-S3-WROOM-2-N32R16V，32 MB Octal Flash、16 MB Octal PSRAM |
| 屏幕 | 微雪 4.26 英寸黑白 SSD1677，800×480；[购买页](https://www.waveshare.com/product/displays/e-paper/4.26inch-e-paper-hat.htm)、[原版说明](https://www.waveshare.com/wiki/4.26inch_e-Paper_HAT_Manual)、[乐鑫开发板说明](https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp32-s3-devkitc-1/user_guide_v1.0.html) |
| 固件目标 | 只使用 `epd_426_ssd1677_s3_n32r16`；仓库中其他历史环境不等于已验证支持 |
| 电池 | `2000 mAh` 一周续航尚无规范实测，不作承诺 |

新安装时**远程新闻源全部关闭**；没有自行启用并配置密钥的新闻源，页面使用四条本地寄语，不会自动调用付费模型。可选的 OpenAI `API 本月消费`是所选组织的 **UTC 自然月**成本，不是 API 余额或个人 Codex 全部消费；DeepSeek 今日 Token 也不是供应商全账户总账。未知值显示 `--`，不冒充零。详见[配置与数据口径](docs/CONFIGURATION.zh-CN.md)和[支持矩阵](docs/SUPPORT_MATRIX.md)。

本机首次打开管理端时需要创建 root 用户，**没有默认用户名或密码**。私有配置、API Key、真实设备地址与运行数据均不随源码分发。Mac 休眠时后端不能按计划运行；设备自行唤醒不代表主机已唤醒。详见[首次使用](docs/FIRST_USE.zh-CN.md)和[主机发布指南](docs/HOST_RUNTIME.zh-CN.md)。

## 许可与状态

项目有权许可的自有代码及四条发行寄语按 [`GPL-3.0-only`](LICENSE) 提供。第三方代码、字体、新闻内容与服务仍受各自条款约束；MiSans 不随仓库、源码 ZIP 或固件二进制分发。参见[许可范围](LICENSE-SCOPE.md)、[第三方通知](THIRD_PARTY_NOTICES.md)和[SBOM](SBOM.json)。

[`v0.1.0-test.2` 说明](docs/releases/v0.1.0-test.2.zh-CN.md)记录了金价后端修复及当时的验收，不包含新固件二进制。[安全问题请私密报告](SECURITY.md)，不要在公开 Issue 贴密钥、配置或设备标识。
