# MiSans 本地导入

## 为什么源码不附带字体

MiSans 原始 OTF、由其生成的 C 头文件、固件内嵌点阵、预览 PNG/BIN 都不进入开源候选包。小米许可允许在软件中使用并要求注明 MiSans，但限制字体文件或副本的再次分发；因此本项目只提供官方下载入口和本地生成工具。

- [MiSans 官方下载与许可页面](https://hyperos.mi.com/font/en/download/)
- [MiSans 官方常见问题](https://hyperos.mi.com/font/en/faq/)
- [MiSans 官方许可协议 PDF](https://hyperos.mi.com/font-download/MiSans%E5%AD%97%E4%BD%93%E7%9F%A5%E8%AF%86%E4%BA%A7%E6%9D%83%E8%AE%B8%E5%8F%AF%E5%8D%8F%E8%AE%AE.pdf)

请自行阅读并接受官方许可。本仓库不是字体再分发方，也不替代法律审查。

## 导入

下载并解压官方字体后运行：

```bash
shared/backend/.venv/bin/python shared/tools/import_misans.py \
  --regular /你的路径/MiSans-Regular.otf \
  --bold /你的路径/MiSans-Bold.otf
```

先只校验可增加 `--check-only`。工具会检查路径、符号链接、扩展名、文件头、32 MiB 大小上限、字体家族、Regular/Bold 字重、版本和面板所需字形；错误使用中文返回。原始字体保存到 `.local/fonts/misans/`，权限为 `0600`；生成的头文件写入固件源码并由 `.gitignore` 排除。全过程不上传云端，不进入诊断或候选包。

当前本机仅做了校验：MiSans Regular/Bold 均识别为 4.003，所需字形通过。没有重生成或替换现有设备字库。

