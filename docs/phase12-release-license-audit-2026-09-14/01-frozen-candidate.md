# 冻结候选与可复现性

## 审查前基线

- 目录：`release-candidate/InkSight-Source/`
- ZIP：`release-candidate/InkSight-Source.zip`
- ZIP SHA-256：`5874b9762cbd59b945a5b4dbb9bd6d5904ae5b84288bd0e3bd8492bafe0c95aa`
- 允许列表源文件：251
- ZIP 文件：255
- 审查时目录文件：259
- ZIP 与目录差异：4，全部为候选目录内后续测试生成的 `.pytest_cache` 文件。
- ZIP 路径穿越、符号链接、嵌套归档：均为 0；顶层 `LICENSE` 缺失；`PUBLICATION-BLOCKED.md` 存在。

这证明旧 ZIP 本身未被那次测试污染，但旧候选目录不是封存对象，目录与 ZIP 已分叉。不能再用旧目录或旧 SHA 作为发布依据。

## 修复后的构建契约

`shared/tools/release/build_candidate.py` 只从显式允许列表复制文件，并在临时目录完成扫描和压缩。随后把 ZIP 解压到新的临时目录，再比较每个文件的 SHA-256、检查路径穿越、重复路径、符号链接、嵌套归档及隐私规则；全部通过才原子替换正式候选目录和 ZIP。

测试只能在 ZIP 的全新解压副本中运行，不能在封存候选目录中直接运行。外部收据 `InkSight-Source.zip.receipt.json` 记录源清单数量、打包文件数量、候选树摘要、ZIP SHA-256 和全新解压结果，避免把 ZIP 自身哈希写入 ZIP 造成循环。

## 审查后候选

- 允许列表源文件：273
- 候选目录与 ZIP 文件：各 277，逐文件 SHA-256 完全一致。
- 连续两次从同一源树构建的 ZIP SHA-256 一致。
- 候选树和 ZIP 全新解压树的隐私扫描：均为 0 项。
- ZIP 路径穿越、重复路径、符号链接、嵌套归档、意外二进制：均为 0 项。
- 随包专项测试：从最终 ZIP 全新解压后 10 项通过。

最终候选树 SHA-256 和 ZIP SHA-256 只写入 ZIP 外部的 `InkSight-Source.zip.receipt.json` 与 `InkSight-Source.zip.sha256`，不写回 ZIP 内，避免摘要自引用。无论隐私扫描是否为 0，许可证/权属门禁未清零前都保持 `publication_ready=false`。
