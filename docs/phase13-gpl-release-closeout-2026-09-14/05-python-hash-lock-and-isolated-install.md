# Python 哈希锁与隔离安装

日期：2026-09-14

## 锁定基线

- 输入：`shared/backend/requirements.txt`
- 输出：`requirements-py39-macos-arm64.lock`
- 解释器：CPython 3.9
- 平台：macOS ARM64
- 解析工具：uv `pip compile --generate-hashes`
- 锁文件：86 个包、1,757 行、1,502 个 SHA-256 制品哈希
- 锁文件 SHA-256：`1d6d1d9fa81a4c65299be0fbb56ca7370ce6a2514aeaab7e116a25088cf5269d`

## 已执行验证

同一锁文件在新的 Python 3.9 虚拟环境中用 uv 严格按哈希安装，86 个包均成功安装。锁文件没有 `file://`、本机绝对路径或可编辑安装引用。

首次全量回归在全新环境中发现原声明漏掉 `sxtwl`：7 项农历/节气测试失败。PyPI 的 sxtwl 2.0.7 没有 macOS ARM64 wheel，且其源码包在该平台构建时缺少 `src/JD.cpp`。本阶段因此将日期实现切换为纯 Python、MIT 许可的 `lunar-python==1.4.8`，重新生成锁文件后日期专项 31 项和后端全量 832 项均通过。这个过程证明锁文件确实覆盖了干净环境，而不是复用旧环境中的隐式依赖。

最终发布候选还需在 ZIP 全新解压副本中重复一次 pip 哈希安装与应用导入；结果记录在 `07-candidate-and-tests.md` 和 ZIP 外部收据中。

## 支持边界

该锁文件不是 Linux、Windows、Intel macOS 或其他 Python 版本的通用承诺。其他平台应从 `shared/backend/requirements.txt` 解析并生成独立锁文件后验收。

`opuslib` 的 Python 包可安装，但运行 Opus 解码需要系统动态库 `libopus`。缺少时应用会关闭可选的 Opus 能力；这不等于系统依赖已经被 Python 锁文件包含。

`requirements.lock.txt` 是本机依赖闭包审计快照，仅用于 SBOM 对照，不能替代上述带哈希锁文件。
