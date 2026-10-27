# 下载公开测试版

[文档目录](README.md) · [Mac App使用](APP_DESKTOP.zh-CN.md) · [升级](UPGRADE.zh-CN.md)

当前是 **[v0.1.0-test.17 公开测试版（Pre-release）](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/tag/v0.1.0-test.17)**。普通用户先下载下面的Mac App；不要从源码ZIP寻找安装器，也不要依赖GitHub的latest跳转选择预发布版。

| 文件 | 用途 |
|---|---|
| [InkSight-macOS-arm64.zip](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.17/InkSight-macOS-arm64.zip) | **完整普通Mac App**；Apple Silicon / macOS26.5+，实际测试26.5.2，内置CPython3.11.15 |
| [InkSight-macOS-arm64.zip.sha256](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.17/InkSight-macOS-arm64.zip.sha256) | App ZIP校验 |
| [InkSight-macOS-arm64.zip.receipt.json](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.17/InkSight-macOS-arm64.zip.receipt.json) | App源码对应、解压/签名/依赖核对收据，不是Apple公证 |
| [InkSight-Source.zip](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.17/InkSight-Source.zip) | 经清单整理及扫描的开发者源码，**不是安装器** |
| [InkSight-Source.zip.sha256](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.17/InkSight-Source.zip.sha256) | 源码ZIP校验 |
| [InkSight-Source.zip.receipt.json](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.17/InkSight-Source.zip.receipt.json) | 源码构建与全新解压核对收据；构建器不代替所有者发布授权 |
| [InkSight-Source.manifest.json](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.17/InkSight-Source.manifest.json) | 源文件清单与哈希 |
| [RELEASE-MANIFEST.json](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/download/v0.1.0-test.17/RELEASE-MANIFEST.json) | 本次tag/提交与资产哈希对应关系 |

GitHub另有自动生成的Source code zip/tar.gz，它们不是上述项目验证资产。没有dmg/pkg、Intel/Windows/Linux安装器、预编译固件bin或MiSans。App ad-hoc签名，**未经Apple Developer ID签名或公证**；不能承诺Gatekeeper首次无提示运行，不要关闭系统安全保护。

将App ZIP与同版校验文件放在一起，进入下载目录执行：

```bash
shasum -a 256 -c InkSight-macOS-arm64.zip.sha256
```

确认OK后用macOS归档实用工具或 `ditto -x -k InkSight-macOS-arm64.zip <目标目录>` 解压完整App，按[App指南](APP_DESKTOP.zh-CN.md)安装。已有用户先正常退出并保留最新数据，再替换相同路径的完整App。不要仅拷贝主程序、改签名包内部源码，或混用QA/旧版校验。失败时停止并重新下载对应版本。

源码用户另行校验并解压：

```bash
shasum -a 256 -c InkSight-Source.zip.sha256
unzip InkSight-Source.zip
cd InkSight-Source
```

App不要求用户安装系统Python或创建root。配置/Wi-Fi/WebDAV需自行填写；MiSans只从[小米官方页面](https://hyperos.mi.com/font/en/download/)合法取得并本地导入。首次固件工具下载需联网并明确启动。源码新准备中心用Python3.11；Python3.9仅保留早期源码后端路径。

已有兼容固件不为匹配App版本重复烧录；需新设备端功能时先核对能力。硬件购买与原版说明见[首页](../README.md)，支持范围和未验收项见[test.17说明](releases/v0.1.0-test.17.zh-CN.md)。历史[test.9](https://github.com/NuoYe-CCnC/InkSight-GLHF/releases/tag/v0.1.0-test.9)资产保留，不作为当前默认推荐。
