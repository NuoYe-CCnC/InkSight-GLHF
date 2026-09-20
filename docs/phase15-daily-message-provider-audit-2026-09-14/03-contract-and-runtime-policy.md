# 适配契约与运行策略

`shared/backend/core/daily_message_provider.py` 定义了候选源元数据、解析器、显示/缓存门禁和未来获准来源的单次获取入口。当前所有候选均为 `release_eligible=false`、`cache_allowed=false`，因此 `fetch_approved()` 会在打开网络前失败。

标准字段：

- `id`：远端稳定 ID；缺失时由来源、原始文本计算，不伪造作者。
- `text` / `lang`：保留原文，不自动翻译、不粗暴截断；中文屏只接收 `lang=zh`。
- `source` / `source_url`：实际提供者和端点。
- `author`：只有上游明确返回时记录，否则为 `null`。
- `content_license` / `license_evidence_url`：文本许可结论与证据页，不能拿 API 代码许可证代替。
- `fetched_at`：UTC epoch 获取时间。
- `release_eligible` / `cache_allowed`：两个独立的显式许可门禁。

不可信响应必须为 UTF-8 JSON，最多 64 KiB；文本拒绝 HTML、控制字符、换行和超出画面预算的内容。超长内容整体拒绝，不截断引用。适配层不把文本送给模型，也不会暗用 DeepSeek 翻译。

未来若有来源通过许可复核，运行策略应为：北京时间同日/同一期稳定选择，后端最多每日一次或按更低频率获取；缓存前再次检查 `cache_allowed`；遵守响应限流并使用有界退避 `0/60/300` 秒；合法缓存不存在时显示诚实的无内容状态，不回退到未核权利文本。
