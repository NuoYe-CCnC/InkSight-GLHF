# Security policy / 安全政策

## 当前状态 / Current status

当前安全修复以最新公开测试版 `v0.1.0-test.1` 为基线。后端 API `1.1.0` 不是项目发布版本号。

Security fixes currently target the latest public test release, `v0.1.0-test.1`. Backend API `1.1.0` is not the project release version.

## 报告敏感问题 / Reporting sensitive issues

不要在公开 Issue、讨论、PR、日志或截图中披露可利用细节、密钥、数据库、真实 MAC、Wi-Fi、云端凭据、Admin Key 或个人数据。

请优先使用 GitHub 的[私密漏洞报告入口](https://github.com/NuoYe-CCnC/InkSight-GLHF/security/advisories/new)。如果该入口临时不可用，请不要公开细节；等待入口恢复，或仅通过已有的私密联系请求一个安全接收方式。一般公开 Issue 只适合不含敏感细节的安全加固建议。

Do not disclose exploit details, secrets, databases, real MAC addresses, Wi-Fi/cloud credentials, Admin Keys, or personal data in public issues, discussions, pull requests, logs, or screenshots.

Prefer GitHub's [private vulnerability reporting form](https://github.com/NuoYe-CCnC/InkSight-GLHF/security/advisories/new). If the form is temporarily unavailable, do not publish details; wait for the private channel to return or use an existing private contact only to request a secure intake route. Public issues are suitable only for non-sensitive hardening suggestions.

## 报告内容 / What to include privately

- 受影响版本、组件与环境；
- 最小复现步骤和影响；
- 已做的脱敏处理；
- 建议缓解方式（如有）。

Include the affected version/component/environment, minimal reproduction, impact, sanitization performed, and any suggested mitigation. No response-time or remediation deadline is promised in this test-stage policy.

## 使用方责任 / Operator responsibilities

- 公开仓库只使用示例配置；真实 `*.json` 私密配置、`.env`、数据库、状态与备份不得提交。
- OpenAI Admin Key 只保存在本机后端，不进入固件或公开载荷。
- 远程新闻源默认关闭，启用前逐源审查条款。
- 不要绕过固件目标、Flash 容量、分区、空白板或一次性确认检查。
- 定期轮换暴露过的任何凭据；仅删除 Git 历史中的字符串并不足以使密钥失效。

Use examples only in public, keep all real secrets/state out of Git, keep Admin Keys backend-local, review each remote source before enabling it, do not bypass flash safety checks, and rotate any exposed credential.
