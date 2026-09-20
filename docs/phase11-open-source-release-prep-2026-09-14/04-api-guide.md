# API 与账户配置指南

## DeepSeek 新闻

新闻密钥在本机管理端写入并单独验证。未配置密钥时使用本地每日寄语；已配置但鉴权失败、等待验证或网络错误时不会冒充“未配置”，也不会自动切换到另一把付费密钥。

默认模型为 `deepseek-v4-flash`，默认输入/输出上限 4000/500 Token，每期最多 5 次。每次调用前先持久化预留；超时或结果不确定也计一次。管理端允许用户设置正整数次数，不额外强加 5 次上限。

## DeepSeek 今日 Token

屏幕使用两路证据：InkSight 自身调用返回的 Token 直接汇总；余额下降达到 0.1 元时按当前价格估算。显示两者中较高的实测/估算值，并标记数据新鲜度。它不是供应商账户全部 Token 的官方计量。

## OpenAI API 月费用

必须使用 Organization Owner 创建的 Admin API Key；普通 OpenAI API Key、Codex 登录令牌和后台 `ADMIN_TOKEN` 均不能替代。后台调用官方 `GET /v1/organization/costs`，按 UTC 自然月完整翻页，仅完整结果可发布。官方参考：

- [Organization costs API](https://developers.openai.com/api/reference/python/resources/admin/subresources/organization/subresources/usage)
- [Admin API keys](https://developers.openai.com/api/reference/python/resources/admin/subresources/organization/subresources/admin_api_keys)

用户确认当前设备曾显示 `API 本月消费 0.00 USD`。这只证明当时屏幕显示链路成立，不证明持续轮询、跨月、离线沿用或补拉已经完成实机验收。

## Codex

套餐名称、续订状态和日期为手动资料；窗口用量、重置机会与点数余额取决于本机登录账户可返回的数据。未知保持 `--`。API 余额与 Codex 点数是不同体系，不互相转换。

