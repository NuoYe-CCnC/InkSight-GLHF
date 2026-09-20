# 内容来源与外部服务矩阵

核对日期：2026-09-14。下表是工程发布决策，不是法律意见；条款可能变化，部署者开启来源前应再次核对。

| 来源/服务 | 当前主体或条款证据 | 工程结论 | 新发行默认 | 部署者动作 |
|---|---|---|---|---|
| TechCrunch RSS | TechCrunch RSS Terms of Use；页面要求仅展示 feed 内容、归因并链接原文，且不得改动 feed 内容 | 自动生成中文摘要超出已确认的原样展示边界 | 关闭 | 取得适用许可后再开启 |
| The Verge | The Next Media Company Terms，2026-07-08 生效，明确包含 The Verge；限制自动/手工数据挖掘、监控、缓存、提取、复制和分发 | 自动采集、缓存和摘要属于高风险用途 | 关闭 | 取得适用许可后再开启 |
| IT之家 RSS | 有 RSS 入口；未确认公开 AI 改写/再发布授权 | 证据不足 | 关闭 | 核对当前转载与自动化条款 |
| 量子位 RSS | 可访问 feed；未确认公开 AI 改写/再发布授权 | 可读取不等于可改写发布 | 关闭 | 核对当前条款或取得许可 |
| Ars Technica RSS | 官方提供 RSS 页面；未据此确认公开 AI 改写/再发布授权 | 证据不足 | 关闭 | 核对当前条款或取得许可 |
| OpenAI News RSS | 官方新闻 feed；网站内容权利与 API 数据处理是不同问题 | 未确认新闻正文可由第三方自动改写发布 | 关闭 | 核对网站内容条款后开启 |
| DeepSeek API | DeepSeek Open Platform Terms | 模型服务条款不替代新闻内容授权；输入内容、输出核验和最终发布由部署者负责 | 无密钥、未验证均不调用 | 自行注册、接受条款、保管密钥并提供所需告知 |
| OpenAI API | OpenAI API 数据控制文档：API 数据默认不用于训练，除非组织选择加入；滥用监控日志通常可保留提示与响应最多 30 天，部分功能保存应用状态 | 数据处理说明不构成输入内容的版权许可，也不证明 AI 输出归属 | 可选、默认不配置 | 按实际端点与账户资格核对保留设置 |
| CC0-1.0 | Creative Commons CC0 deed 明示不影响他人可能拥有的权利 | 只有实际权利人才能作出有效放弃；AI 生成记录本身不能证明权利链 | `daily_messages` 仍待权利人确认 | 确认自有权利，或替换、删除、采用正确许可 |

## 权威页面

- TechCrunch RSS Terms: <https://techcrunch.com/rss-terms-of-use/>
- The Next Media Company Terms: <https://www.thenextmediacompany.com/terms.html>
- OpenAI API data controls: <https://developers.openai.com/api/docs/guides/your-data>
- DeepSeek Open Platform Terms: <https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html>
- Creative Commons CC0: <https://creativecommons.org/publicdomain/zero/1.0/>

来源链接、抓取时间和“AI 摘要”标识是必要透明度信息，但归因不等于许可。
