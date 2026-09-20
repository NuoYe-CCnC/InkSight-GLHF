# Contributing / 参与贡献

感谢关注 InkSight-GLHF。首个测试版只承诺[支持矩阵](docs/SUPPORT_MATRIX.md)中的硬件与主机基线；提交其他平台或屏幕改动时，请清楚标注“未实机验证”。

## 提交前 / Before submitting

1. 先搜索现有 Issue；Bug 使用 Bug 模板，功能建议使用 Feature 模板。
2. 不要提交真实 Wi-Fi、API Key、Admin Key、云端密码、数据库、状态文件、含凭据日志、真实 MAC、字体文件、生成字库、预览图或固件二进制。
3. 保留第三方版权头和许可证；新增来源、字体或内容时同时说明来源、版本、许可和再分发边界。
4. 新安装默认必须保持远程新闻源关闭；不要用接口软件许可证代替逐条内容权利审查。
5. 文档命令从发行根目录运行，使用相对路径和脱敏占位符。

Please search existing issues, use the provided templates, keep all secrets and private artifacts out of submissions, preserve third-party notices, document provenance/licensing for new material, keep remote news disabled by default, and use release-root-relative commands with sanitized placeholders.

## 本地检查 / Local checks

在已安装依赖的 macOS ARM64 / Python 3.9 环境中：

```bash
cd shared/backend
.venv/bin/python -m pytest -q
```

涉及发行文件时还应从仓库根运行候选构建器；它只生成本地候选，不会上传：

```bash
shared/backend/.venv/bin/python shared/tools/release/build_candidate.py
```

固件改动至少构建 `epd_426_ssd1677_s3_n32r16`。只有实际连接已验证硬件并完成对应步骤时，才写“实机通过”；模拟、编译和截图不能替代实机结果。

Run the backend suite for software changes, rebuild the local candidate for release-file changes, and build the verified PlatformIO target for firmware changes. Label simulation, compilation, and physical-device results separately.

## 变更说明 / Change notes

提交说明请包含：问题、方案、影响文件、测试命令与结果、未测范围、是否改变配置/网络/付费调用/设备写入。界面改动附脱敏截图；不要上传用户数据。

Describe the problem, approach, affected files, test commands/results, untested scope, and whether configuration, network access, paid calls, or device writes change. Screenshots must be sanitized.

项目自有贡献预计按 `GPL-3.0-only` 进入项目；第三方材料仍按其原许可。提交即表示你有权提供该贡献，但本文件不是额外的权利保证或法律意见。

Project-owned contributions are expected under `GPL-3.0-only`; third-party material keeps its own license. By contributing, you represent that you may provide the contribution. This is not an additional legal warranty or legal advice.
