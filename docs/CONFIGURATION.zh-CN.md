# 配置什么、数据从哪里来

[文档目录](README.md) · [首次使用](FIRST_USE.zh-CN.md) · [配置字段说明](../shared/config/README.md)

首次运行**不需要填写任何付费 API Key**。两份配置由 `inksight_config.py init` 在本机创建；管理端采用“草稿 → 校验 → 确认应用”，不要把示例文件或自己的真实配置提交到公开仓库。

| 你希望启用的能力 | 用户要提供什么 | 留空时 |
|---|---|---|
| 本机网页管理 | App 从 Is →“打开配置”进入受保护会话；独立源码部署才创建 root 用户与至少 12 位密码 | App 不需要 root；源码无默认账号/密码 |
| 实体屏联网 | 自己的 Wi-Fi 网络；构建时同步到设备，网络清单是完整快照 | 不具备联网条件；改 Wi-Fi 后须重新构建/刷写 |
| 坚果云/WebDAV 发布 | HTTPS 目录、账号、应用密码；设备和主机须指向同一位置；发布器还需本机 `ADMIN_TOKEN` 与设备 MAC | 后端本地运行，跨网络设备不会自动收到新文档 |
| 远程新闻 | 逐源确认条款、主动启用来源、提供新闻用 DeepSeek API Key、设定调用/Token 预算 | **远程新闻源全部关闭**，使用四条本地寄语，不会自动调用付费模型 |
| DeepSeek 余额/Token | 对应 DeepSeek Key；自行在[官方平台](https://platform.deepseek.com/)管理 | 未知值为 `--`，不是余额为零 |
| Codex 窗口/点数 | 本机安装并登录 Codex CLI，主动启用[主机采集](HOST_RUNTIME.zh-CN.md)；显示点数还须打开相应开关 | 无法实时采集，未知不伪装成零 |
| OpenAI 组织月消费 | 组织 Owner 创建的 Admin API Key、主动打开显示开关并在管理端验证 | 默认不显示；普通 API Key、Codex 登录令牌和 `ADMIN_TOKEN` 均不能代替 |
| 设备字库 | 自行从[小米官方 MiSans 页面](https://hyperos.mi.com/font/en/download/)取得并阅读许可，再在本机导入 | 后端可启动；不能构建本目标的可用固件 |

普通设置存于 `shared/config/inksight_config.json`；Wi-Fi、API Key、坚果云凭据等私密设置存于 `shared/config/inksight_secrets.json`（权限 `0600`）。运行状态/数据库在 `shared/backend/`，不是可公开的示例。网页管理端可更改部分字段，但“保存草稿”不等于已应用；改变设备编译输入也不会自动改写现有固件。详细优先级、旧配置迁移和 Wi-Fi NVS 替换规则见[配置字段说明](../shared/config/README.md)。

数据口径也要分清：DeepSeek “今日 Token”取本项目 API 回传量与余额变化估算中的较高值，并非服务商全账户官方总量；OpenAI `API 本月消费`来自所选组织的 Organization Costs API，按 **UTC 自然月**统计，不是预付余额或个人 Codex 订阅账单。屏幕上的金价是 XAUS 国际现货换算，仅供参考，并非国内金店报价或交易建议。数据可处于缓存、陈旧或未知状态；不要凭 `--` 推断为 0。

如果你只是想先验证界面，保持默认关闭状态即可；不要为了“看到新闻”而把未审查来源、真实密钥或私有图片贴到公开 Issue。需要帮助请先看[故障排查](TROUBLESHOOTING.zh-CN.md)。
