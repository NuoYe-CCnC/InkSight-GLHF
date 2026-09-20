# 第三方组件与许可证缺口

## 已识别

- 固件直接包含的 `EPD_4in26.cpp`、`DEV_Config.cpp`：文件头标注 Waveshare team，并带 MIT 风格许可正文；发布时必须保留原头部。
- GxEPD2 1.6.9：本地标签文件和上游许可证检查支持 `GPL-3.0-only`，不能把许可证附录示例中的 “or later” 当成项目授权。
- WebSockets 2.7.3：源文件头支持 `LGPL-2.1-or-later`。
- Adafruit GFX 1.12.6：BSD。
- Adafruit BusIO 1.17.4：MIT（传递依赖）。
- ArduinoJson 7.4.3：MIT。
- NimBLE-Arduino 2.5.1：Apache-2.0。
- Python 依赖的精确范围记录在 `requirements.txt`；安装包各自保留其上游许可。
- 后端渲染字体 Noto Serif SC、Inter、Lora 由安装脚本从 Google Fonts 获取，不打包字体文件。无明确许可证的文泉驿镜像已从公共安装流程移除。
- MiSans 不分发，见 `05-misans-local-build.md`。

## 顶层许可证建议

由于首个受支持固件直接链接 `GPL-3.0-only` 的 GxEPD2，整体采用 `GPL-3.0-only` 是最直接的工程方案。也可以把后端与固件拆分许可，或替换该依赖后再评估宽松许可证；细节见 Phase 12 审计。这里不替用户作最终选择。

## 发布阻塞项

1. 用户尚未选择顶层许可证，候选包故意不含 `LICENSE`。
2. NOTICE/SBOM 已生成，但顶层选择、Python 哈希锁、图标排除、内置文案权属和外部服务条款仍需最终确认。
3. `epd_wft.h` 标注改编自 Waveshare，但文件本身没有完整许可头；在保留或发布前需追溯上游文件。
4. 历史静态画作、图标、旧预览和所有字体二进制均未进入候选包，需在权属明确后才能逐项加入。
