# InkSight-GLHF 安装、配置、构建与恢复

[English](INSTALLATION.en.md) · [返回 README](../README.md)

本文只描述首个测试版已验证的 macOS ARM64 + Python 3.9 + ESP32-S3 N32R16V + 4.26 英寸 SSD1677 800×480 路径。仓库中的其他 PlatformIO 环境不代表已支持硬件。

## 1. 准备

- macOS ARM64 主机、Python 3.9。
- ESP32-S3-DevKitC-1-N32R16V（32 MB Octal Flash、16 MB Octal PSRAM）。
- 微雪 4.26 英寸黑白 800×480 SSD1677 墨水屏。
- 可传数据的 USB 线和短杜邦线。
- 构建固件时需要 PlatformIO Core；版本化平台由 `platform = espressif32@7.1.0` 固定，PlatformIO CLI 本身尚未提供跨平台哈希锁。

发行包不包含任何生产数据库、状态、缓存、Wi-Fi、API Key、账号、真实 MAC、字体或固件二进制。

## 2. 安装并启动后端

从解压后的发行目录根执行：

```bash
python3.9 -m venv shared/backend/.venv
shared/backend/.venv/bin/python -m pip install --require-hashes \
  -r requirements-py39-macos-arm64.lock
shared/backend/.venv/bin/python shared/tools/inksight_config.py init
shared/backend/run-backend.sh
```

`init` 只在两份新配置均不存在时创建空白配置对。若检测到旧 `manual_settings.json`，它会拒绝覆盖；先执行：

```bash
shared/backend/.venv/bin/python shared/tools/inksight_config.py migrate --dry-run
```

确认提案无误后才应用：

```bash
shared/backend/.venv/bin/python shared/tools/inksight_config.py migrate \
  --apply --confirm-activate
```

浏览器打开 `http://127.0.0.1:8080/`。首次启动自行创建首位 root 用户，密码至少 12 位。没有默认用户名或密码。

若需要更接近设备的网页预览字体，可单独运行：

```bash
shared/backend/.venv/bin/python shared/backend/scripts/setup_fonts.py
```

该脚本从 Google Fonts 获取 Noto Serif SC、Inter 和 Lora 的 OFL 字体；这与固件 MiSans 导入是两件不同的事。

## 3. 配置与网页管理

公开配置分为：

- `shared/config/inksight_config.json`：普通参数。
- `shared/config/inksight_secrets.json`：Wi-Fi、API Key、云端凭据，权限应为 `0600`。
- `shared/backend/state/`：运行状态，不应手工合并到配置，也不应提交。

管理端使用“草稿 → 校验 → 确认应用”。已有配置不会因升级源码或再次运行 `init` 被静默覆盖。命令行可只读检查：

```bash
shared/backend/.venv/bin/python shared/tools/inksight_config.py validate
shared/backend/.venv/bin/python shared/tools/inksight_config.py show-effective
```

新安装远程新闻源全部关闭。仅当你逐源审查条款、打开相应开关并配置新闻 DeepSeek Key 后才会请求远程新闻。无 Key/无来源时使用四条本地寄语；四条会重复，不承诺全年不重复。

可选数据口径：

- DeepSeek 今日 Token：InkSight 自身调用回传 Token 与余额变化估算两路取较高值，不是供应商账户全量官方统计。
- OpenAI `API 本月消费`：所选组织的 UTC 自然月消费，不是余额，也不是个人 Codex 全部开销。仅接受 Organization Owner 创建的 Admin API Key。
- OpenAI Admin Key 只保存在本机后端私密配置中，不会进入固件或公开设备载荷；普通 API Key、Codex 登录令牌和后台 `ADMIN_TOKEN` 不能替代。

## 4. MiSans 本地导入

MiSans 不随仓库或发行包分发。请在[小米官方 MiSans 下载页](https://hyperos.mi.com/font/en/download/)自行下载并阅读许可，然后运行：

```bash
shared/backend/.venv/bin/python shared/tools/import_misans.py \
  --regular /path/to/MiSans-Regular.otf \
  --bold /path/to/MiSans-Bold.otf \
  --check-only

shared/backend/.venv/bin/python shared/tools/import_misans.py \
  --regular /path/to/MiSans-Regular.otf \
  --bold /path/to/MiSans-Bold.otf
```

第一次只校验；第二次在本机 `.local/fonts/misans/` 保存私有副本，并生成被 `.gitignore` 排除的固件字库头文件。不要提交或分享原字体、生成头、字形缓存、预览和构建出的固件；软件按 GPL 开源不代表第三方字体获得再分发许可。

## 5. 接线

| 墨水屏 | ESP32-S3 |
|---|---|
| VCC | 3V3 |
| GND | GND |
| DIN | GPIO11 |
| CLK | GPIO12 |
| CS | GPIO10 |
| DC | GPIO13 |
| RST | GPIO14 |
| BUSY | GPIO4 |
| PWR（9 针 HAT） | GPIO5 |

上电前核对 3.3 V、共地、BUSY 和 PWR。不要把其他板型或彩色 4.26 英寸屏当作已验证替代品。

## 6. 安装 PlatformIO 并构建

```bash
python3.9 -m venv .pio-venv
.pio-venv/bin/python -m pip install platformio
export PLATFORMIO_CORE_DIR="$PWD/.pio-core"
export INK_SSID=""
export INK_PASS=""
export INK_EXTRA_WIFI=""
export INK_SERVER=""
export CLOUD_BASE_URL=""
export CLOUD_USER=""
export CLOUD_PASS=""
export INKSIGHT_BUILD_ID="local-test"
.pio-venv/bin/python -m platformio run -d shared/firmware \
  -e epd_426_ssd1677_s3_n32r16
```

首次运行会从上游下载 PlatformIO 平台和依赖，不应假设新电脑已有本机缓存。将 Wi-Fi 或云端密钥作为真实构建输入前，优先在网页管理端的私密配置中填写，避免把秘密写进文档、Issue 或 shell 历史。

## 7. 刷写与升级

推荐在本机管理端使用“固件”流程。它只接受固定目标，构建时快照配置，刷写前重新识别 ESP32-S3 与 32 MB Flash，并要求一次性确认：

- `fresh`：只接受关键区域确实为空白的新板；写入 bootloader、分区表、OTA 初始化与 app0，不执行全盘擦除。
- `update`：只接受分区表完全匹配的已有设备；先备份 NVS/OTA/目标应用区，只写未运行的 OTA 应用分区，校验后切换启动项，保留 NVS。

USB 写入是高风险操作。不要凭设备名称猜串口，也不要对未知板运行擦除。开发者如明确理解 PlatformIO 直接上传与受控流程的差异，可使用：

```bash
.pio-venv/bin/python -m platformio run -d shared/firmware \
  -e epd_426_ssd1677_s3_n32r16 -t upload \
  --upload-port /dev/cu.YOUR_DEVICE
```

这条直接上传命令不提供网页受控流程的空白板检查、备份和心跳确认。

## 8. 运行、休眠与恢复

- 前台运行：`shared/backend/run-backend.sh`。停止用 `Ctrl-C`。
- 主机休眠时后端不能保证定时执行。唤醒恢复只处理当前可补跑任务，不重放所有错过时刻。
- 设备的定时唤醒与深度睡眠独立于主机；设备唤醒不代表后端已醒。
- 2000 mAh 一周续航尚未完成规范实测，不应作为测试版承诺。

管理员忘记密码时，在后端所在电脑运行：

```bash
shared/backend/.venv/bin/python shared/tools/admin_recovery.py
```

配置迁移、草稿应用和私密配置更新有本地事务/备份保护，但不能替代用户自己的离线备份。升级前备份私有配置、数据库和 `shared/backend/state/`；不要把备份提交到 Git。

## 9. 故障判断

- 管理端显示 `--`：数据未知、未配置或没有完整快照，不等于 0。
- 没有新闻：确认这是默认行为；只有启用且授权的来源才会联网。
- 构建缺字库：先完成 MiSans 导入，不要从他人候选包复制生成头。
- 主机睡眠后漏刊：保持主机唤醒，或迁移到经过审查的常在线环境。
- 刷写被拒绝：不要绕过硬件、容量、分区或空白检查；先确认目标是否真的是已支持组合。
