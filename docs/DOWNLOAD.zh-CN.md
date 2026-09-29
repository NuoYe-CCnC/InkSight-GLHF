# 下载公开测试版

[文档目录](README.md) · [首次使用](FIRST_USE.zh-CN.md)

当前公开版本是 [`v0.1.0-test.2`](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/tag/v0.1.0-test.2)，标记为 **Pre-release**。在该页的 Assets 中选择：

| 文件 | 用途 |
|---|---|
| [`InkSight-Source.zip`](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.2/InkSight-Source.zip) | 项目整理、扫描过的源码候选，供自行部署与构建；**不是一键安装器** |
| [`InkSight-Source.zip.sha256`](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.2/InkSight-Source.zip.sha256) | 同一 ZIP 的 SHA-256 校验文件 |
| [`InkSight-Source.zip.receipt.json`](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.2/InkSight-Source.zip.receipt.json) | 发行收据，供核对内容与构建信息 |

三个项目资产之外，GitHub 还自动显示 `Source code (zip/tar.gz)`；它们不是项目验证过的源码候选，也不是安装器。请不要把两种 ZIP 混用。此版**没有** `.app`、`.dmg`、`.pkg`、Windows/Linux 安装包、预编译固件 `.bin` 或 MiSans 字体。固件只能在本机合法导入字体后按[安装指南](INSTALLATION.zh-CN.md)自行构建；实体设备需要接线及受控刷写。

将 ZIP 与 `.sha256` 放在同一目录，在 macOS 终端进入该目录后运行：

```bash
shasum -a 256 -c InkSight-Source.zip.sha256
unzip InkSight-Source.zip
cd InkSight-Source
```

第一条必须报告 `InkSight-Source.zip: OK`。失败时停止，不运行、不刷写，重新下载两份同版本文件。解压后的入口是 `README.md` 和 `docs/INSTALLATION.zh-CN.md`。想用最新 `main` 文档也可以从仓库网页阅读；**test.2 已发布的 ZIP 不会因后续文档提交而自动更新**。

已验证的组合是 macOS ARM64 + Python 3.9 后端、ESP32-S3 N32R16V 与微雪 4.26 英寸黑白 SSD1677 屏；其他平台或面板请先看[支持矩阵](SUPPORT_MATRIX.md)。硬件购买/原版说明在[项目首页](../README.md)，没有项目方授权的第三方字体或新闻内容被打包。
