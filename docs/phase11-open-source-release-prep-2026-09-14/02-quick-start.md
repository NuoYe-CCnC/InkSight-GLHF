# macOS 快速开始（首个支持组合）

## 支持范围

- 主机：macOS；Windows、Linux 暂未完成整套验收。
- 控制器：ESP32-S3-WROOM-2-N32R16V（32 MB Octal Flash、16 MB Octal PSRAM）。
- 屏幕：微雪 4.26 英寸黑白墨水屏，800×480，SSD1677。
- 固件环境：`epd_426_ssd1677_s3_n32r16`。

硬件参考与购买入口：

- [微雪 4.26 英寸黑白屏 HAT（SKU 26376）](https://www.waveshare.com/product/displays/e-paper/4.26inch-e-paper-hat.htm)
- [微雪 4.26 英寸屏官方说明](https://www.waveshare.com/wiki/4.26inch_e-Paper_HAT_Manual)
- [微雪原版用户手册 PDF](https://files.waveshare.com/wiki/4.26inch-e-Paper-HAT/4.26inch_e-Paper_User_Manual.pdf)
- [乐鑫 N32R16V 开发板说明与购买入口](https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp32-s3-devkitc-1/user_guide_v1.0.html)

购买时必须核对“黑白 800×480”和“N32R16V”。彩色 4.26 英寸 G 版不是当前支持型号。

## 安装

```bash
cd InkSight-Source
python3 -m venv shared/backend/.venv
shared/backend/.venv/bin/pip install -r shared/backend/requirements.txt
shared/backend/.venv/bin/python shared/backend/scripts/setup_fonts.py
shared/backend/.venv/bin/python shared/tools/inksight_config.py init
```

MiSans 不随源码分发。按 `05-misans-local-build.md` 从小米官网下载并在本机导入，然后启动：

```bash
shared/backend/run-backend.sh
```

浏览器打开 `http://127.0.0.1:8080/`。首次启动自行创建 root 用户名和至少 12 位密码；项目没有默认管理员密码。

不填写任何付费 API Key 也能启动：新闻页使用本地每日寄语，费用、余额和设备联网状态诚实显示为未配置或未知。
