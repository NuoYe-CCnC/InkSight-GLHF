# 候选包与测试记录

日期：2026-09-14

## 最终候选

- 目录：`release-candidate/InkSight-Source`
- ZIP：`release-candidate/InkSight-Source.zip`
- 校验文件：`release-candidate/InkSight-Source.zip.sha256`
- 机器可读收据：`release-candidate/InkSight-Source.zip.receipt.json`

ZIP 的最终 SHA-256 只写在 ZIP 外部校验文件和收据中，避免把自身摘要写回 ZIP 造成循环变化。

## 验收项目

最终重建前后的独立验证结果：

- 源工作区后端全量测试：832 项通过；2 个既有依赖警告（系统 LibreSSL/urllib3 与 DashScope 弃用提示）。
- GPL/哈希锁/候选门禁专项：15 项通过；日期依赖修复后日期专项 31 项通过。
- ZIP 全新解压后的 Python 3.9 安装：pip 21.2.4 使用 `--require-hashes --no-cache-dir`，86 个包安装成功。
- ZIP 全新解压后的随包专项测试：15 项通过；测试未在封存候选目录中运行。
- 应用导入：`api.index`、日期模块和语音模块成功；系统无 `libopus` 时 `opus_available=False`，按设计关闭可选 Opus 能力。
- 隔离 HTTP：设置 `INKSIGHT_OFFLINE_STARTUP=1` 后 `/api/health` 返回 200 和版本 1.1.0；首次 root 状态显示需要初始化；恶意跨站 Origin 返回 403。后台网络轮询关闭，生产 8080 未重启。
- MiSans：原始候选不含字体和生成头；只在 ZIP 的临时解压副本中从本机字体导入，未改动候选或 ZIP。
- 固件：临时解压副本的 `epd_426_ssd1677_s3_n32r16` 构建成功。PlatformIO espressif32 7.1.0、Arduino-ESP32 `3.20017.241212+sha.dcc1105b`；RAM 96,648/327,680，应用 Flash 3,857,825/6,291,456。只编译，未烧录。
- 固件依赖：六个精确版本库在临时解压副本中重新解析；框架和工具链使用本机 PlatformIO 缓存，因此不能替代全新电脑零缓存复验。
- 候选目录与 ZIP 全新解压树逐文件一致；路径穿越、重复条目、符号链接和隐私扫描均为 0。
- 最终候选连续构建两次的目录树与 ZIP SHA-256 一致；具体计数和摘要以 ZIP 外部 `.sha256` 与 `.receipt.json` 为准。

候选技术测试通过也不会自动把 `publication_ready` 改成 true；发布门禁见 `08-publish-gates-and-owner-confirmations.md`。
