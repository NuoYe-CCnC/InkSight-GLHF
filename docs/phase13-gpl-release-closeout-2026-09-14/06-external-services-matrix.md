# 外部服务与内容来源矩阵

日期：2026-09-14

代码分发与运行时调用是两件事。候选包只提供配置入口；运营者仍需自行注册、接受条款、保管密钥、提供隐私说明，并确认内容使用方式。以下是工程审查，不是法律意见；条款会变化，公开部署前应重新核对。

| 服务/来源 | 项目用途 | 当前工程结论 | 发布/运行要求 |
|---|---|---|---|
| OpenAI API | 费用查询或可选模型能力 | 可由用户自行配置；密钥必须留在服务端 | 只给占位符；说明数据保留与管理员权限；不随包提供密钥 |
| DeepSeek API | 新闻摘要/模型调用 | 可集成，但输入、输出、终端用户与隐私责任由下游承担 | 密钥只放服务端；不得暗示官方合作；AI 输出应核验并适当标识 |
| XAUS | CNY/克现货金价和日内数据 | 官方页面允许合理的个人、仪表盘及开源使用，要求至少缓存 30 秒；价格仅供参考 | 保持 30 秒以上缓存；显示数据时间/来源和非交易报价属性；高频使用先联系 |
| gold-api.com | XAUS 响应可能标记的底层报价来源 | 当前不是代码直接请求目标，但需保留来源链 | 不把免费或准确性描述成永久保证；公开页面保留来源说明 |
| 坚果云 WebDAV | 用户配置/同步 | 只提供用户自行配置的 WebDAV | 地址用 `https://dav.jianguoyun.com/dav/`；使用第三方应用密码，不用登录密码；不代用户开户或接受条款 |
| Ars Technica RSS | 新闻候选 | 官方提供 RSS 订阅页，但未据此证明可公开 AI 改写/再发布 | 公开运营前确认用途；至少保留来源和原文链接 |
| IT之家 RSS | 新闻候选 | 官方提供 RSS，但站点对转载另有授权/出处要求 | 公开 AI 摘要前取得确认；保留来源和原文链接 |
| 量子位 RSS | 新闻候选 | 可读取 feed 不等于允许公开改写 | 条款证据不足，需运营者确认或关闭 |
| TechCrunch RSS | 新闻候选 | RSS 条款要求归因、链接原文且不得改动 feed 内容；AI 摘要属于高风险用途 | 未获许可前禁止用于公开 AI 改写，或默认关闭该来源 |
| The Verge | 新闻候选 | 当前适用主体为 The Next Media Company；2026-07-08 生效条款对自动/手工数据挖掘、监控、缓存、提取、复制和分发有明确限制 | 未获许可前默认关闭自动抓取/摘要 |
| OpenAI News RSS | 新闻候选 | 官网内容仍受网站条款约束 | 公开摘要前核对允许范围并保留来源链接 |

## 权威页面

- OpenAI API 数据控制：<https://developers.openai.com/api/docs/guides/your-data>
- DeepSeek 开放平台条款：<https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html>
- DeepSeek API 文档：<https://api-docs.deepseek.com/api/deepseek-api/>
- XAUS API：<https://xaus.com/api/>
- gold-api.com 条款：<https://gold-api.com/terms>
- 坚果云服务条款：<https://help.jianguoyun.com/?page_id=490>
- 坚果云 WebDAV 帮助：<https://help.jianguoyun.com/?p=5677>
- Ars Technica RSS：<https://arstechnica.com/rss-feeds/>
- IT之家 RSS 说明：<https://www.ithome.com/0/000/037.htm>
- TechCrunch RSS 条款：<https://techcrunch.com/rss-terms-of-use/>
- The Next Media Company 使用条款（包含 The Verge，2026-07-08 生效）：<https://www.thenextmediacompany.com/terms.html>

## OpenAI 数据提示

OpenAI 官方当前说明：API 数据默认不用于训练，除非组织明确选择加入；滥用监控日志通常可保留提示和响应最多 30 天，部分端点还会保存应用状态。具体例外与资格以官方数据控制页面为准。

## 推荐默认策略

发行默认配置已将全部远程新闻源关闭；现有用户的明确选择在升级时保留，生产配置不会被本轮改写。运营者逐源确认条款后可单独开启；Web 管理页保留来源、原文链接、时间和 AI 摘要标识，墨水屏布局不变。
