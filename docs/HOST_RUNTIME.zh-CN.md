# 主机采集与发布（可选）

[返回安装指南](INSTALLATION.zh-CN.md)

后端和 ESP32 是两条独立运行链。只启动后端时，管理页与本地寄语可用；若要让跨网络的设备持续收到新文档，还需在保持唤醒的主机上运行发布器。本页使用通用源码入口，不依赖开发者个人的 LaunchAgent、绝对路径或私有设备清单。

## 准备

1. 完成[安装指南](INSTALLATION.zh-CN.md)，启动后端。
2. 在已有的 `shared/config/inksight_secrets.json` 中填写 `cloud.base_url`（HTTPS WebDAV 目录）、`cloud.user`、`cloud.password`。不要把这个文件加入 Git。
3. 在本机私有的 `shared/backend/.env` 设置随机 `ADMIN_TOKEN`，重启后端使其生效。该令牌只用于本机管理接口，不是 OpenAI API Key、Codex 登录令牌或 WebDAV 密码。不要在命令行参数中粘贴令牌。
4. 查到目标 ESP32 的 12 位十六进制 MAC。命令中只传 MAC，不传密码。云端目录与设备固件中的 WebDAV 配置必须指向同一位置。

以下命令均从仓库根目录执行。`AA:BB:CC:DD:EE:FF` 是占位符，须替换为自己的设备 MAC：

```bash
shared/backend/.venv/bin/python shared/tools/host_cycle.py \
  --mac AA:BB:CC:DD:EE:FF --once
```

确认单次发布可用后，去掉 `--once` 运行前台循环，默认每 60 秒检查一次；每 10 分钟重新上传一次心跳。停用时按 `Ctrl-C`。主机休眠时它不会继续执行。

## 可选采集与设备请求

若本机已安装并登录 Codex CLI，可加 `--codex`。该选项只读取本机 `account/rateLimits/read`，不会发起模型对话；读取结果必须含真正的 7 日窗口才会提交。失败时保留旧额度，最多每 10 分钟重试一次；本机管理页总览会显示失败分类、上次成功和重试时间。Codex 登录状态与订阅状态由官方客户端管理，本项目不读取或发布认证材料。

```bash
shared/backend/.venv/bin/python shared/tools/host_cycle.py \
  --mac AA:BB:CC:DD:EE:FF --codex
```

`--request-queues` 会处理设备在 WebDAV 写入的金价补拉与资讯到期请求。它是**额外的主动开关**：只在你已审查新闻源授权、配置 API 密钥与调用预算后启用，可能触发收费的服务端请求。没有该开关，发布器不会主动处理这些队列。

首次测试建议先保持 `--once` 且不开 `--request-queues`。若需要只运行某个组件，源码入口分别是 `cloud_publish.py --operator-mac ... --once`、`codex_quota_probe.py --json`、`gold_requests.py --operator-mac ...` 和 `news_requests.py --operator-mac ...`。前两者只读/发布；后两者可能触发后端已有的受限补取流程。

## 边界与故障判断

- `shared/config/inksight_secrets.json`、`shared/backend/.env`、运行状态、数据库、MiSans 派生字库都在忽略列表内；不要手动提交。
- 本机管理页的采集健康属于本机诊断，不进入设备屏幕或 WebDAV 文档。
- 管理页显示“数据陈旧”不等于额度为零，也不代表订阅已过期；检查 Codex CLI 登录、主机唤醒、`ADMIN_TOKEN` 和网络，再等待一次重试。
- 未安装 MiSans 字库的公开检出可运行后端与测试，但固件构建仍需按安装指南自行导入字体。日期栏会先用保守宽度估算，导入后改用真实字宽。
- 此通用入口只覆盖已验证的单主机、单 WebDAV 设备路径；不承诺 Windows/Linux、多发布主机仲裁或断电后的历史事件重放。
