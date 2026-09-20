# InkSight-GLHF 首次 GitHub 测试版发布清单

本清单记录 `v0.1.0-test.1` 的发布决定与机械检查。用户已明确授权上传完整公开候选、将仓库设为 Public，并创建预发行版；最终 GitHub 界面动作仍按执行环境的确认规则完成。

## 已确定

- [x] 仓库名：`InkSight-GLHF`
- [x] 顶层许可：`GPL-3.0-only`
- [x] 四条本地寄语逐字确认并按项目 GPL 范围进入发行覆盖层
- [x] MiSans、生成字库、预览、固件二进制、生产配置和运行数据不随源码候选分发
- [x] 新安装远程新闻源默认关闭

## 建议的仓库简介

- 中文：`面向 ESP32-S3 与 4.26 英寸墨水屏的本地双页信息面板，提供 AI 用量、新闻/金价展示、网页配置与固件构建。`
- English: `A local two-page information panel for ESP32-S3 and a 4.26-inch e-paper display, with AI usage, news/gold views, web configuration, and firmware builds.`

## 已确认的发布信息

- [x] GitHub owner：`NuoYe-CCnC`
- [x] 最终仓库 URL：`https://github.com/NuoYe-CCnC/InkSight-GLHF`
- [x] 首个版本号：`v0.1.0-test.1`
- [x] 仓库公开，并明确标为测试版
- [x] 启用并核对 GitHub Private Vulnerability Reporting
- [x] Release 标题：`InkSight-GLHF v0.1.0-test.1`；标为 pre-release
- [ ] 上传前在 GitHub 界面核对 README、LICENSE、Issue 模板与 Security 入口

## 上传前机械检查

- [ ] 只从 `release-candidate/InkSight-Source/` 或其 ZIP 创建仓库，不从生产工作区整体拖拽
- [ ] 使用 ZIP 外部 `.sha256` 与 `.receipt.json` 核对最终资产
- [ ] `PRIVACY-SCAN.json` findings 为 0；无危险路径、符号链接、嵌套归档
- [ ] 无 `.env`、真实配置、密钥、数据库、状态、备份、缓存、真实 MAC 或个人绝对路径
- [ ] 无 MiSans 原字体、生成字库头、预览、固件二进制
- [ ] 候选运行寄语只有四条确认稿，许可字段为 `GPL-3.0-only`
- [ ] README 和 Release Notes 使用最终仓库 URL，不含 owner/版本占位符
- [ ] Release Notes 的“已验证 / 用户确认 / 未测”仍与最终结果一致

## 发布后首轮检查

- [ ] 核对 GitHub 页面、Release 资产名称与 SHA-256；本次按用户要求跳过“远程下载→安装→运行”验收
- [ ] 检查相对链接、LICENSE、NOTICE、SBOM 和 Issue 模板渲染
- [ ] 不使用真实付费 API 做公开烟雾测试；使用离线/模拟测试
- [ ] 若发现秘密，先撤回资产并轮换凭据，不能只删除文件或改历史
