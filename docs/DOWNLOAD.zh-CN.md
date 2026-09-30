# 下载公开测试版

[文档目录](README.md) · [首次使用](FIRST_USE.zh-CN.md)

当前公开版本是 [`v0.1.0-test.4`](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/tag/v0.1.0-test.4)，标记为 **Pre-release**。在该页的 Assets 中选择：

| 文件 | 用途 |
|---|---|
| [`InkSight-macOS-arm64.zip`](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.4/InkSight-macOS-arm64.zip) | 独立 Mac 应用测试包；Apple Silicon、macOS 26.5.2 已验证，未获 Apple 公证 |
| [`InkSight-macOS-arm64.zip.sha256`](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.4/InkSight-macOS-arm64.zip.sha256) | App ZIP 的 SHA-256 校验文件 |
| [`InkSight-Source.zip`](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.4/InkSight-Source.zip) | 项目整理、扫描过的源码候选，供自行部署与构建；**不是安装器** |
| [`InkSight-Source.zip.sha256`](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.4/InkSight-Source.zip.sha256) | 源码 ZIP 的 SHA-256 校验文件 |
| [`InkSight-Source.zip.receipt.json`](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.4/InkSight-Source.zip.receipt.json) | 源码发行收据 |

上述项目资产之外，GitHub 还自动显示 `Source code (zip/tar.gz)`；它们不是项目验证过的源码候选，也不是安装器。请不要把两种源码 ZIP 混用。此版**没有** `.dmg`、`.pkg`、Windows/Linux 安装包、预编译固件 `.bin` 或 MiSans 字体。固件只能在本机合法导入字体后按[安装指南](INSTALLATION.zh-CN.md)自行构建；实体设备需要接线及受控刷写。

将对应的 ZIP 与 `.sha256` 放在同一目录，在 macOS 终端进入该目录后运行。App 用户执行：

```bash
shasum -a 256 -c InkSight-macOS-arm64.zip.sha256
```

报告 `OK` 后解压并按[Mac App 使用说明](APP_DESKTOP.zh-CN.md)安装。源码用户执行：

```bash
shasum -a 256 -c InkSight-Source.zip.sha256
unzip InkSight-Source.zip
cd InkSight-Source
```

校验必须报告 `OK`。失败时停止，不运行、不刷写，重新下载两份同版本文件。源码解压后的入口是 `README.md` 和 `docs/INSTALLATION.zh-CN.md`；已经发布的旧版 ZIP 不会因后续文档提交而自动更新。

App 已验证组合是 Apple Silicon、macOS 26.5.2 和内置 Python 3.11.15；源码后端另有 macOS ARM64 + Python 3.9 路径。ESP32-S3 N32R16V 与微雪 4.26 英寸黑白 SSD1677 屏的实机路径见[支持矩阵](SUPPORT_MATRIX.md)，本版没有重新刷屏验收。硬件购买/原版说明在[项目首页](../README.md)，没有项目方授权的第三方字体或新闻内容被打包。安装前还应阅读[`test.4` 验证与限制](releases/v0.1.0-test.4.zh-CN.md)。
