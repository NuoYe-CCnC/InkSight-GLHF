# InkSight-GLHF

[English](README.en.md) · [文档目录](docs/README.md)

面向 ESP32-S3 与 4.26 英寸黑白墨水屏的双页信息面板：`AI 用量`显示可获得的 Codex 窗口、DeepSeek 余额与本项目 Token；`今日关注`显示本地寄语或经用户启用的新闻，以及 XAUS 国际现货金价换算。项目包含本机后端、网页管理端和固件源码，不是 OpenAI、DeepSeek、小米或硬件厂商的官方产品。

> 当前是公开测试版，提供已在 Apple Silicon、macOS 26.5.2 隔离验证的独立 `InkSight.app`，也保留源码包。App **尚未获得 Apple Developer ID 签名或公证**，首次打开可能出现系统安全提示；不是无提示正式安装包。仍不提供可直接刷写的固件二进制。要使用实体屏，还须自行接线、合法取得 MiSans 并构建/刷写固件。没有已获公开许可的实屏照片，本页暂不放可能暴露私人余额或账号的图片。

## 从这里开始

1. [下载测试版 `v0.1.0-test.4`](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/tag/v0.1.0-test.4)——先看[文件选择与校验](docs/DOWNLOAD.zh-CN.md)。Mac 应用和供开发者用的 `InkSight-Source.zip` 是不同资产。
2. [Mac 应用首次使用](docs/APP_DESKTOP.zh-CN.md)——内含运行环境，不必单独装 Python；源码部署继续看[源码首次使用](docs/FIRST_USE.zh-CN.md)。
3. [升级已有设备](docs/UPGRADE.zh-CN.md)——先分清仅更新后端与需要刷写固件的版本，备份后再操作。

只想了解运行中的配置和日常检查，可从[用户文档目录](docs/README.md)进入；贡献代码请走[开发者路径](CONTRIBUTING.md)。

## 已验证范围与默认行为

| 部分 | 本测试版的边界 |
|---|---|
| 主机 | 独立 App：Apple Silicon、macOS 26.5.2、内含 Python 3.11.15；源码后端：macOS ARM64、Python 3.9。其他 macOS 版本、Windows、Linux、Intel Mac 尚未完成同等级验收 |
| 控制器 | ESP32-S3-DevKitC-1-N32R16V / ESP32-S3-WROOM-2-N32R16V，32 MB Octal Flash、16 MB Octal PSRAM |
| 屏幕 | 微雪 4.26 英寸黑白 SSD1677，800×480；[购买页](https://www.waveshare.com/product/displays/e-paper/4.26inch-e-paper-hat.htm)、[原版说明](https://www.waveshare.com/wiki/4.26inch_e-Paper_HAT_Manual)、[乐鑫开发板说明](https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp32-s3-devkitc-1/user_guide_v1.0.html) |
| 固件目标 | 只使用 `epd_426_ssd1677_s3_n32r16`；仓库中其他历史环境不等于已验证支持 |
| 电池 | `2000 mAh` 一周续航尚无规范实测，不作承诺 |

新安装时**远程新闻源全部关闭**；没有自行启用并配置密钥的新闻源，页面使用四条本地寄语，不会自动调用付费模型。可选的 OpenAI `API 本月消费`是所选组织的 **UTC 自然月**成本，不是 API 余额或个人 Codex 全部消费；DeepSeek 今日 Token 也不是供应商全账户总账。未知值显示 `--`，不冒充零。详见[配置与数据口径](docs/CONFIGURATION.zh-CN.md)和[支持矩阵](docs/SUPPORT_MATRIX.md)。

本机首次打开管理端时需要创建 root 用户，**没有默认用户名或密码**。私有配置、API Key、真实设备地址与运行数据均不随源码分发。Mac 休眠时后端不能按计划运行；设备自行唤醒不代表主机已唤醒。详见[首次使用](docs/FIRST_USE.zh-CN.md)和[主机发布指南](docs/HOST_RUNTIME.zh-CN.md)。

## 许可与状态

项目有权许可的自有代码及四条发行寄语按 [`GPL-3.0-only`](LICENSE) 提供。第三方代码、字体、新闻内容与服务仍受各自条款约束；MiSans 不随仓库、源码 ZIP 或固件二进制分发。参见[许可范围](LICENSE-SCOPE.md)、[第三方通知](THIRD_PARTY_NOTICES.md)和[SBOM](SBOM.json)。

[`v0.1.0-test.4` 说明](docs/releases/v0.1.0-test.4.zh-CN.md)记录了 Codex 到期质量、Mac App 实机发布和实验性固件回退的验证边界；仍不包含新固件二进制。[安全问题请私密报告](SECURITY.md)，不要在公开 Issue 贴密钥、配置或设备标识。
