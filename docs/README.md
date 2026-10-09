# 文档目录 / Documentation

[返回项目首页](../README.md) · [English README](../README.en.md)

本目录先区分使用者与开发者，再区分**已可验证**和**尚未提供**的交付物。当前 `v0.1.0-test.9` 有独立 Mac 测试应用和源码 ZIP；前者未获 Apple 公证，后者不是安装器。仍没有预编译固件。以前发布的测试版附件不会追溯更改。

## 使用者路径

| 你要做什么 | 从这里开始 |
|---|---|
| 选择下载文件、核验完整性 | [下载说明](DOWNLOAD.zh-CN.md) |
| 在已验证 Mac 上第一次运行 | [独立 App 首次使用](APP_DESKTOP.zh-CN.md)；走源码安装则看[源码首次使用](FIRST_USE.zh-CN.md)；接线/刷写见[完整安装指南](INSTALLATION.zh-CN.md) |
| 填写 Wi-Fi、坚果云、API Key 与理解用量口径 | [配置与数据口径](CONFIGURATION.zh-CN.md)；字段细节见[配置说明](../shared/config/README.md) |
| 日常发布设备数据 | [主机采集与 WebDAV 发布](HOST_RUNTIME.zh-CN.md) |
| 现有主机/设备升级或回退 | [升级与回退](UPGRADE.zh-CN.md) |
| 首次启动、无新闻、无数据、刷写失败 | [故障排查](TROUBLESHOOTING.zh-CN.md) |
| 了解已验证/用户确认/未验收 | [支持矩阵](SUPPORT_MATRIX.md)与[当前测试版说明](releases/v0.1.0-test.9.zh-CN.md) |
| 菜单栏额度、免密码配置、关闭与退出 | [Is 菜单与本机会话](MENU_BAR.zh-CN.md) |
| 金价折算与期刊状态的口径/升级边界 | [黄金契约](GOLD_UNIFIED.zh-CN.md)、[期刊状态历史记录](NEWS_ISSUE_STATUS.zh-CN.md)；当前验收以 test.9 说明为准 |

英文源码安装详见 [Installation (English)](INSTALLATION.en.md)。其余简明用户页目前以中文为准；没有完成英文翻译的页面会明确保留 `.zh-CN.md` 文件名，不假装已有双语版本。

## 开发者与发布者路径

- [贡献指南](../CONTRIBUTING.md)、[安全政策](../SECURITY.md)、[许可范围](../LICENSE-SCOPE.md)、[第三方通知](../THIRD_PARTY_NOTICES.md)和[SBOM](../SBOM.json)。
- [完整安装、接线、MiSans 导入、构建与受控刷写](INSTALLATION.zh-CN.md)。仓库中 `shared/firmware/platformio.ini` 的默认环境是历史环境，**请始终显式指定**已验证目标 `epd_426_ssd1677_s3_n32r16`。
- [未来版本 Release 模板](RELEASE_TEMPLATE.zh-CN.md)：发布时填写真实平台、资产、校验值、升级/回退和已知问题；模板不是本版已有的安装器承诺。
- [历史阶段记录目录](archive/README.md)：只作为溯源，不是当前操作手册。保留原路径，避免破坏已有链接。
