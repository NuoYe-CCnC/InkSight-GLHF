# 首次使用：先启动本机后端

[文档目录](README.md) · [下载说明](DOWNLOAD.zh-CN.md) · [完整安装/接线/刷写](INSTALLATION.zh-CN.md)

本页是**源码部署**的最短可验证路径，不是一键安装承诺。已验证主机是 macOS ARM64、Python 3.9。先按[下载说明](DOWNLOAD.zh-CN.md)校验并解压 `InkSight-Source.zip`，再在解压所得 `InkSight-Source` 目录执行下列命令。首次安装依赖需要网络，过程可能花几分钟。

1. 用 `uname -m` 确认输出 `arm64`。用 `python3.9 --version` 检查 Python 3.9；若没有 `python3.9` 命令，再试 `python3 --version`。**只有确认它输出 3.9.x，才可在下面第一行把 `python3.9` 换成 `python3`**。这台 Mac 上的隔离验收使用的是 `python3` 3.9.6。
2. 从源码根目录执行：

   ```bash
   python3.9 -m venv shared/backend/.venv
   shared/backend/.venv/bin/python -m pip install --require-hashes \
     -r requirements-py39-macos-arm64.lock
   shared/backend/.venv/bin/python shared/tools/inksight_config.py init
   shared/backend/run-backend.sh
   ```

3. 后端在前台运行。**在同一台 Mac** 的浏览器打开 `http://127.0.0.1:8080/`，按页面提示创建首位 root 管理员；没有默认用户名或密码，密码至少 12 位。首次创建仅允许从本机回环地址访问。也可另开终端访问 `http://127.0.0.1:8080/api/health`，正常返回 `{"status":"ok",...}`；其中后端 API 版本号不是本项目 Release 版本号。结束服务按 `Ctrl-C`。

`init` 只在两份新配置都不存在时创建空白文件，**不会覆盖旧配置**。若出现旧版 `manual_settings.json`，先按[升级指南](UPGRADE.zh-CN.md)做备份及 `migrate --dry-run`，不要清空旧文件来强行初始化。新配置分别是 `shared/config/inksight_config.json` 与私密的 `shared/config/inksight_secrets.json`；运行状态和数据库也在本机，不要上传 GitHub。

首次看到本地寄语或未知值 `--` 是正常的：远程新闻源默认关闭，未填密钥不调用付费模型。健康检查只证明后端启动，不证明金价/额度数据新鲜，更不证明实体屏或云端发布已经工作。若要让设备跨网络收到数据，请再按[配置与数据口径](CONFIGURATION.zh-CN.md)和[主机发布指南](HOST_RUNTIME.zh-CN.md)设置坚果云/WebDAV；若要第一次点亮屏幕，还须按[完整安装指南](INSTALLATION.zh-CN.md)检查接线、导入 MiSans、构建和受控刷写。请勿把空白配置、未接线或未刷写误认为“安装失败”。

后端入口是前台脚本，不会自动注册开机服务。Mac 睡眠期间调度与发布无法保证；本机首次验收建议保持主机唤醒。故障见[排查页](TROUBLESHOOTING.zh-CN.md)。
