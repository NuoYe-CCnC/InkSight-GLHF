# InkSight 用户配置

配置 schema 当前为 v2，并继续采用四层分离：

1. `inksight_config.json`：普通用户参数，不放密码、令牌、SSID、MAC、私有路径或会员日期以外的公开示例数据。
2. `inksight_secrets.json`：Wi-Fi、API Key 与云端认证，文件权限必须为 `0600`。
3. `../backend/state/`：调用账本、已出刊键、缓存时间、退避和锁；不要手工合并到用户配置。
4. `../firmware/src/config.h` 与 `../firmware/platformio.ini`：板型、屏幕、引脚、驱动、PSRAM 等固定硬件定义。

`manual_settings.json` 是旧版单文件私有配置，包含 Wi-Fi、会员资料、显示开关、服务密钥、坚果云地址/账号/应用密码、Token 初始值和新闻每期调用次数。没有执行迁移时系统继续读取它；新架构在同一管理页编辑，落盘时按普通项/私密项分为上述两个文件。迁移不会自动发生。手填 API 余额和 Codex 点数已废弃，不再出现在示例或运行时回退中。

## 离线工具

以下命令都不会联网、调用模型、启动调度器或烧录设备：

```bash
# 仅供没有旧配置的新用户初始化；两份文件成对创建
shared/backend/.venv/bin/python shared/tools/inksight_config.py init

# 校验候选配置，不激活
shared/backend/.venv/bin/python shared/tools/inksight_config.py validate

# 显示脱敏后的有效配置、来源、指纹和生效提示
shared/backend/.venv/bin/python shared/tools/inksight_config.py show-effective

# 只查看旧配置迁移会改哪些字段
shared/backend/.venv/bin/python shared/tools/inksight_config.py migrate --dry-run
```

真实迁移需要同时写明应用和激活确认：

```bash
shared/backend/.venv/bin/python shared/tools/inksight_config.py migrate \
  --apply --confirm-activate
```

这只写本地配置文件，不会重启后端或烧录设备。首次迁移只创建原本都不存在的目标；后续受控配置对编辑会先备份原内容到私有目录 `shared/config/backups/`。旧 `manual_settings.json` 和运行状态均保留。

`init` 不再为旧用户写入完整默认值。检测到 `manual_settings.json` 时会拒绝初始化并提示先运行 `migrate --dry-run`，因为默认普通值和空秘密会在优先级上遮盖旧值。只存在一份新文件也会拒绝自动补齐；两份新文件都存在时仅返回 no-op，绝不覆盖。

迁移默认只允许首次创建完整配置对。两份目标已经与旧配置提案完全一致时返回 `already-migrated`，不重写、不更新时间、不制造备份；任何迁移后编辑、损坏、未知版本/字段或只存在一份目标都会以脱敏字段路径拒绝，绝不再次拿旧文件覆盖。当前没有“强制覆盖”入口。

## 优先级

普通字段：内置默认值 → 旧 `manual_settings.json` 兼容映射 → `inksight_config.json` 显式覆盖。

秘密字段：旧 `manual_settings.json` → `inksight_secrets.json` → 对应环境变量仅在最终值为空时兜底。现有环境变量为 `DEEPSEEK_API_KEY`、`NEWS_DEEPSEEK_API_KEY`、`GOLDAPI_KEY`。Wi-Fi 没有环境变量兜底，烧录命令显式传入的 `INK_SSID` 等仍由烧录脚本优先采用。

`false`、`0`、`null` 和空数组不会被当成“缺省”。秘密文件中的空字符串表示未配置；若旧值被新空值覆盖，才会检查环境变量兜底。

## 配置对事务

初始化、首次迁移和新私密文件的 Wi-Fi 编辑共用同一配置对事务。写入前先校验整组，然后以 `0600` 私密事务日志记录完整旧组和新组及 SHA-256，依次写入两份目标，最后原子提交。所有有效读取都会检查事务日志并做双次稳定快照：只能得到完整旧组或完整新组，不接受中间混合组。

普通失败会在返回前回滚；进程在任意写入点被中断时，下一次有效读取会按 durable journal 确定性恢复：`prepared` 回到完整旧组，`committed` 完成完整新组。正常读取仍不写文件；只有检测到中断日志时才执行恢复。事务日志和锁文件均被 `.gitignore` 排除，且不属于运行状态或用户应手工编辑的内容。

## 生效边界

- 会员、面板开关、Token 初始值、日期显示偏好和 API Key 已接入加载器。
- `news_digest.max_input_tokens` / `max_output_tokens` 由每个新期次读取并形成不可变快照；安全替换配置后下一期自动采用，无需重新烧录设备。正在重试的同一期继续使用旧快照，避免初稿与修订口径不一致。
- `device_policy.activity_windows`、`adaptive_check`、`page_switch`、`time_sync` 与重试退避通过结构化载荷供设备读取；设备需运行包含 schema v2 支持的新固件。
- `device_policy.network` 的连接/HTTP 总预算在编译时注入，因为设备连网前无法读取云端载荷；修改后必须重新编译并烧录才生效。
- Wi-Fi 与云端拉取凭据也属于编译输入；修改主机配置不会改写设备 NVS，也不会自动烧录。
- 管理端填写任意 Wi-Fi 行时，该列表是下一次构建的完整快照，顺序即优先级。新固件首次启动会按构建编号一次性用该快照替换 NVS 中的旧 Wi-Fi 列表，因此省略的旧网络会被删除；同一固件后续重启不会反复覆盖设备端编辑。全部清空只会清除本机配置，因设备将失去联网入口，管理端不会为零网络配置创建可刷写构建。
- `reserved` 仅保留尚未接线的新闻、金价、行情等未来契约。其 `enabled` 必须保持 `false`，避免“配置成功但没有效果”。
- 缺少可选服务密钥只显示 `configured: false`，不会把未知余额写成 `0`，也不会导致其它功能停止。

## v1 到 v2

v1 公共配置和秘密文件仍可读取：加载器只在内存中升级为 v2，不修改原文件，也不会把 v1 中明确标为未启用的 `reserved.activity_policy` / `reserved.device_runtime` 静默激活。要长期编辑新策略，请以 `inksight_config.example.json` 的 v2 结构创建或迁移配置。

## 常用显示开关

- `show_codex_credits`、`show_openai_api_info` 默认均为 `false`。
- `show_openai_api_info=true` 时显示自动读取的 `API 本月消费`。口径是 UTC 自然月、整个所选组织；只有 `/v1/organization/costs` 全部分页完成后才发布数值，失败时沿用同组织同月份的上次完整快照并标记陈旧。
- `show_codex_credits=true` 时显示本机已登录 Codex 账户自动返回的 `点数余额`。它与手动重置机会是两个独立字段；缺失仍是未知，`unlimited` 作为状态显示，不换算成数字或 USD。
- `services.openai_admin_api_key` 只能在本机管理端写入，不能使用普通 `OPENAI_API_KEY`、DeepSeek 密钥、后台 `ADMIN_TOKEN` 或 Codex 登录令牌替代。换钥后必须在管理端明确点击验证，之后才允许自动刷新。
- 当两个右侧开关都关闭且 Codex 新鲜数据明确返回“手动重置 0 次”时，底部两行自动改为 `API 本月消费` / `点数余额`。未知或陈旧的 0 不触发切换。
- `show_reset_opportunities` 控制手动重置次数和到期信息，默认保留当前已确认显示。
- `show_weekday`、`show_lunar`、`show_solar_terms`、`show_festivals` 分别控制右上日期栏内容；关闭只删除对应信息，不移动页面骨架。

公开仓库只提交 `*.example.json`。本地配置、秘密、备份已被 `.gitignore` 排除。
# 第三阶段可调项

- `news_digest.schedules`：最多 12 个启用时刻/日，稳定 `id`、标题、时间、适用日型；相邻时刻至少 30 分钟。全部关闭是合法配置。
- `news_digest.sources` / `topic_preferences` / `precollect_minutes`：资讯来源、偏好和提前采集。
- `news_digest.allow_missed_catchup=true` 时，只补当前一期：工作日窗口为 08:55 至 12:55、12:55 至 16:55、16:55 至 22:00；休息日默认 08:55 至 20:00。边界属于下一期。`workday_cutoff`、`restday_cutoff` 和可选的单期 `cutoff` 只允许同日时间，跨午夜补跑不受支持。旧 `retry_window_minutes` 只在关闭新补跑规则时兼容生效。
- `news_digest.max_input_tokens` / `max_output_tokens`：单次完整模型请求的输入与输出上限，默认 `4000` / `500`。输入统计覆盖 system、user、候选、结构约束和修订说明；采用 UTF-8 字节数加消息框架开销作为不低估的保守上界，不把中文字数当 token。当前唯一支持模型 `deepseek-v4-flash` 的已核实官方边界为 1M 上下文、最大输出 384K；两项必须为正整数，输入至少 2300、输出至少 128，且输入加预留输出不得超过上下文。提高数值会增加潜在费用，但不会改变 800×480 页面字数、行数或字号。
- `news_digest.max_calls_per_issue`：每期最多调用次数，默认 `5`。只接受正整数，不设固定上限；超时或结果不确定也会计一次，不能借重试绕过。数值越高，潜在费用越高。
- `news_digest.monthly_budget_cny` 是旧配置兼容字段，`null` 为新默认。旧数值和费用账本只用于统计，不再充当 10/8/9 元执行门槛。
- 未配置 `services.news_deepseek_api_key` 时使用仓库内本地“每日寄语”，不产生模型调用，也不受生成 token 预算影响。生产环境仍保留原有 12 条数据；公开候选由构建器确定性覆盖为四条发布者确认、按 `GPL-3.0-only` 处理的项目寄语，不分发旧 CC0 标记语料。阶段 15 调研的网络寄语 API 全部默认关闭，不能用接口代码许可证替代逐条文本权利。已填写但停用、待验证、鉴权失败或临时网络错误不会被当作“未配置”。
- `gold_refresh.scheduled_enabled`：控制北京时间每个整点和半点的 XAUS 后端刷新。
- `gold_refresh.wake_enabled`：控制设备每次联网后的独立唤醒刷新；关闭定时刷新不会连带关闭它。
- 自建后端模式使用设备令牌保护的直连端点；坚果云模式使用既有 Basic Auth 请求队列，两者都不是公开匿名接口。
- `services.goldapi_key` 是旧版兼容保留字段，第三阶段 XAUS 实现不读取它。
- 旧 GoldAPI/多令牌/国内金价方案已废弃，只保留字段和历史文档供迁移审计；运行时没有回退路径。
