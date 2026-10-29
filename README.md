# InkSight-GLHF

[English](README.en.md) · [使用文档](docs/README.md) · [test.19 更新说明](docs/releases/v0.1.0-test.19.zh-CN.md)

**macOS 菜单栏与 ESP32-S3 墨水屏看板。** 把 AI 用量、科技简报和金价放在桌边；从 Is 菜单管理本机服务、查看额度与可选的实验性重置预告。test.19 将预告同步放入关注页右底栏，保持正文完整，不因预告切页。

**[下载 Mac App · Apple Silicon](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.19/InkSight-macOS-arm64.zip)** · [测试版与校验文件](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/tag/v0.1.0-test.19)

要求 **Apple Silicon、macOS 26.5+**，实际验证 26.5.2；App 内含 Python 3.11.15。当前为公开测试版，ad-hoc 签名，**无 Apple Developer ID 签名或公证**。实体屏另需下列硬件、接线及本机固件准备；不提供通用预编译固件。

## 界面预览

以下是 test.19 正式渲染器与沿用的隔离界面的**演示数据 / 历史回放**，不是个人账户、实时行情或当前预告。简报文字为本项目自写演示。点击图片可查看原尺寸；前两页保留 800×480 原图。

![AI 用量演示：Codex 窗口、手动重置、DeepSeek 余额与今日 Token](docs/images/ai-usage.png)
AI 用量：查看可获得的额度、重置时间与本项目今日 Token。[800×480 原图](docs/images/ai-usage-800.png)

![今日关注演示：科技简报、两家 AI 用量及金价折算](docs/images/today-brief.png)
今日关注：简报、用量和国际现货金价换算放在一页。[800×480 原图](docs/images/today-brief-800.png)

![Is 原生菜单历史回放：额度星号、实验性重置预告与查看预告原文](docs/images/is-menu.png)
Is 菜单：有效预告附星号，可核对原文；历史回放不代表当前存在预告。

![真实管理页：本地导入字体、准备工具、检查配置、构建和受控刷写入口](docs/images/device-setup.png)
设备准备：步骤与阻塞原因可见。本图未执行 USB 写入，不表示空白板已安装成功。

## 三步开始

1. **下载并打开 App。** 先按[下载校验](docs/DOWNLOAD.zh-CN.md)核对同版 SHA256，在原安装路径放入完整 App；Is →“打开配置”进入本机管理页，无需 root 账号或密码。
2. **配置与准备。** 填写 Wi-Fi、WebDAV 和需要的服务；从小米官方合法取得 MiSans，在[图形准备中心](docs/DEVICE_FIRST_USE.zh-CN.md)本地导入字体、明确点击准备工具并检查配置。默认关闭全部远程新闻源与实验性预告，没有自行启用的新闻源和密钥时显示本地寄语，不自动调用付费模型。
3. **确认设备后构建、刷写。** 按[接线与安装](docs/INSTALLATION.zh-CN.md)确认支持的板卡和屏幕；新板与已有设备使用不同流程。已有设备先备份，受控升级仅写非活动槽并保留设置。普通 App 升级不为版本号重复刷写；**启用关注页底栏预告需要本版固件更新**，详见[升级说明](docs/UPGRADE.zh-CN.md)。

## 硬件与运行条件

| 项目 | 支持范围与原版资料 |
|---|---|
| 控制器 | ESP32-S3-DevKitC-1-N32R16V / ESP32-S3-WROOM-2-N32R16V，32 MB Octal Flash、16 MB Octal PSRAM；[乐鑫原版说明](https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp32-s3-devkitc-1/user_guide_v1.0.html) |
| 墨水屏 | 微雪 4.26 英寸黑白 SSD1677，800×480；[购买页](https://www.waveshare.com/product/displays/e-paper/4.26inch-e-paper-hat.htm)、[原版说明](https://www.waveshare.com/wiki/4.26inch_e-Paper_HAT_Manual) |
| 固件目标 | `epd_426_ssd1677_s3_n32r16`；其他历史环境不等于已验证支持 |
| 主机 | App：Apple Silicon / macOS 26.5+；Mac 需保持唤醒且联网，休眠时不保证采集或发布 |

其他 macOS 版本、Intel Mac、Windows、Linux 尚未完成同等级验收。`2000 mAh` 一周续航没有规范实测，不作承诺。[支持矩阵](docs/SUPPORT_MATRIX.md)列出验证边界。

## 数据、预告与默认行为

- **远程新闻源全部关闭**，需用户逐源审查条款并启用。预告也默认关闭，启用才访问相关公开来源；已有用户开关在升级时保留。
- Codex 来自可获得的本机窗口，不等于 OpenAI API 账单。可选 `API 本月消费`是所选组织的 **UTC 自然月**成本，不是 API 余额；DeepSeek 今日 Token 是本项目直接记录与余额估算按规则汇总的指标，不是官方全账户总账。未知显示 `--`。[配置与数据口径](docs/CONFIGURATION.zh-CN.md)
- test.18 解析本人回复与有界系列上下文、午夜和待定时间，以北京时间展示，并提供原文入口及来源诊断。实验来源可能遗漏或变化，**不保证提前预告、个人适用或到账**，不改变真实额度、重置次数或切页逻辑。[预告 v2](docs/CODEX_FORECAST_V2.zh-CN.md)
- Is 显示本机有效 Codex 7D 缓存的剩余整数百分比，缺失或过期为 `-`；有效实验预告加 `*`。关闭窗口保留后台，退出请用 Is 菜单。源码独立部署需自行创建管理员，**没有默认用户名或密码**。[菜单与本机会话](docs/MENU_BAR.zh-CN.md)

本项目不是 OpenAI、DeepSeek、小米或硬件厂商的官方产品。私有配置、密钥、设备地址、账本和运行状态不公开。[安全问题请私密报告](SECURITY.md)。

## 验证、源码与许可

本版全量回归 **704 通过、0 跳过**，另有一项既有 Dashscope 弃用警告；验收与发布复核以[test.19 更新说明](docs/releases/v0.1.0-test.19.zh-CN.md)的最终日志为准。本机 App 迁移和单实例、现有板备用槽升级/完整回读/联网及保留两组 Wi-Fi 已验证，用户确认底栏刷新且显示正常。空白板、另一台 Mac/Gatekeeper、真实回退、重新登录自启、两页所有状态和长时间切页未据此认定通过。

开发者下载 [InkSight-Source.zip](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.19/InkSight-Source.zip)，它不是安装器；另见[贡献指南](CONTRIBUTING.md)、[源码安装](docs/FIRST_USE.zh-CN.md)与[文档目录](docs/README.md)。

自有代码及四条发行寄语按 [`GPL-3.0-only`](LICENSE) 提供。第三方代码、字体、新闻与服务遵循各自条款；MiSans 原始字体和生成字库不随仓库、App 源模板或源码 ZIP 分发。图片为渲染输出，不含可提取的字体文件。见[许可范围](LICENSE-SCOPE.md)、[第三方通知](THIRD_PARTY_NOTICES.md)、[图片来源](docs/images/README.md)和[SBOM](SBOM.json)。
