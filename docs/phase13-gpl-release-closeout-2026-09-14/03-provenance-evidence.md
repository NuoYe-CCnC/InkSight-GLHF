# 来源证据

日期：2026-09-14

## InkSight 上游

- 权威仓库：<https://github.com/datascale-ai/inksight>
- 核对修订：[`ea73138e8f69b8632d39295afeab32c3560d92c4`](https://github.com/datascale-ai/inksight/commit/ea73138e8f69b8632d39295afeab32c3560d92c4)，提交时间 2026-07-15。
- 许可证：上游仓库的 MIT 正文已保存在 `LICENSES/InkSight-upstream-MIT.txt`。
- 方法：取得该修订的递归文件树，按 Git blob 算法计算本候选文本文件标识，再以路径/内容交叉核对。
- 结果：候选中有 124 个文件与该修订存在精确 blob 匹配。可明确举证的文件包括 `shared/firmware/src/epd_wft.h`、`shared/firmware/src/epd4in2_wft.h`、`shared/backend/core/modes/builtin/daily.json`，以及多个内置模式、后端模块和固件位图/波形表。

这项结果修正了上一阶段对 `epd_wft.h` 来源不足的判断：该文件可以证明与上述 MIT 上游修订逐字节一致。由于本地工作区没有 `.git`，仍不能把该修订宣称为真实 fork 基点，也不能凭匹配反推出其后所有改动的作者与授权。

## Waveshare 4.26 英寸驱动

- 权威仓库：<https://github.com/waveshareteam/e-Paper>
- 核对修订：[`a794fbc39656b0f93938d1ffb3fdc77eaed9e9fc`](https://github.com/waveshareteam/e-Paper/commit/a794fbc39656b0f93938d1ffb3fdc77eaed9e9fc)，提交时间 2026-08-19。
- 对照文件：[`EPD_4in26.c`](https://github.com/waveshareteam/e-Paper/blob/a794fbc39656b0f93938d1ffb3fdc77eaed9e9fc/RaspberryPi_JetsonNano/c/lib/e-Paper/EPD_4in26.c) 及同目录配置头/实现。
- 结果：本地 `EPD_4in26.*`、`DEV_Config.*` 保留 Waveshare 文件中的 MIT 风格许可头；本地实现是面向 Arduino/ESP32 的改编，不是逐字节副本。可观察差异包括忙等待超时保护、平台 GPIO/SPI 适配和控制序列配置差异。

Waveshare 仓库顶层未找到统一许可证正文，因此只依据相关源文件自身保留的许可头陈述，不把整个仓库笼统标成 MIT。

## 资源与文案

- 机器人位图与当前 InkSight 上游修订中的对应资源一致，可随上游 MIT 通知追溯。
- `shared/backend/data/daily_messages.json` 是本地文本，并声明 `CC0-1.0`；只有实际权利人才能作 CC0 放弃/授权，仍需发布者确认。
- MiSans 原始字体、生成头文件及字形产物均不分发；公开工具只指导用户从小米官方来源自行取得并在本机生成。
- 来源未核实的 PNG/JPG 等图片继续排除。

## 结论边界

本文只记录可复核的文件级证据，不伪造提交历史，不推断作者身份，也不构成法律意见。
