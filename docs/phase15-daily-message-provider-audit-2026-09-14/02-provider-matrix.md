# 每日寄语 API 候选矩阵

核对日期：2026-09-14。以下结论是保守工程决策，不是“绝对不侵权”保证。

| 候选 | 官方端点/返回 | 许可与使用证据 | 中文/翻译 | 缓存/限流 | 结论 |
|---|---|---|---|---|---|
| Affirmations | `GET https://www.affirmations.dev`；对象字段 `affirmation` | 官方代码仓库 MIT，但句库可由贡献者增加，代码许可不能自动证明每句文本权利 | 英文；无获许可中文译文 | 官方 README 未说明稳定限流或文本缓存许可 | 不默认启用；仅保留实验解析器 |
| ZenQuotes | `GET /api/today` 或 `/api/random`；数组字段 `q/a/h` | 官方文档要求免费版显示 ZenQuotes 链接；未找到逐条名言与翻译权利证据 | 英文 | 默认每 IP 30 秒 5 次，文档建议缓存，但内容权利仍未核 | 有条件候选，不默认启用 |
| Quotable | `GET /quotes/random`；`_id/content/author` | API 实现为 MIT；数据仓库称 open source，但本轮未找到可复核的数据 LICENSE 文件，不能由代码许可推导名言权利 | 英文 | README 写每 IP 每分钟 180 次 | 不默认启用；本轮 DNS 解析失败 |
| 中国哲学书电子化计划 | `https://api.ctext.org/` JSON API | 官方允许合理离线使用量，但明确现代翻译仍受作者版权保护，实际文本还受作品/版本条件约束 | 古典中文可用，现代翻译不可默认复制 | 匿名访问有限，账户/机构资格不同 | 仅可为经过逐作品、逐版本核验的古典原文另做白名单，不接随机默认 |
| 一言 Hitokoto | `https://v1.hitokoto.cn/?encode=json` | API 代码 Apache-2.0；语句包 AGPL-3.0。官方同时明确语句著作权并非完全由平台持有并提供侵权移除 | 中文为主，含动漫、文学、网络内容 | 官方 QPS 2，建议链接 UUID | 不作为低风险默认来源 |
| 今日诗词 | `https://v2.jinrishici.com/one.json` | 官方仅开放非商业使用，并说明诗词数据来源于网络、可能侵权或错漏；终端 IP/Token 还有隐私责任 | 中文古诗词 | 5 秒每 IP 最多 25 次；推荐每用户独立 Token，不适合本后端共享调用 | 不默认启用 |
| Wikimedia/Wikiquote | MediaWiki API | Wikimedia 文本通常为 CC BY-SA 4.0/GFDL，必须逐页确认、归因、相同方式共享并标注修改；随机页仍可能含现代/合理使用材料 | 有中文项目 | 动态限流；必须标识 User-Agent 并遵守退避 | 可研究“古典作品+固定修订白名单”，通用随机模式不默认启用 |

## 本轮只读连通性

- Affirmations：成功返回 JSON，键为 `affirmation`。
- ZenQuotes `/api/today`：成功返回 JSON 数组，字段为 `a/date/h/i/q`。
- Quotable：DNS 解析失败，因此不能标记为当前可用。
- CText `getstatus`：成功返回 `loggedin/subscriber` 状态字段；未抓取正文。

测试没有批量下载或输出候选句子正文。

## 官方依据

- Affirmations README 与代码许可：<https://github.com/annthurium/affirmations>
- ZenQuotes 文档：<https://docs.zenquotes.io/zenquotes-documentation/>
- Quotable API：<https://github.com/lukePeavey/quotable>
- Quotable 数据仓库：<https://github.com/quotable-io/data>
- CText API：<https://ctext.org/tools/api>；FAQ：<https://ctext.org/faq>
- 一言语句接口：<https://developer.hitokoto.cn/sentence/>；语句包：<https://github.com/hitokoto-osc/sentences-bundle>
- 今日诗词文档及使用协议：<https://www.jinrishici.com/doc/>
- Wikimedia API 使用指引：<https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_API_Usage_Guidelines>
- Wikimedia 使用条款：<https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use/en>
