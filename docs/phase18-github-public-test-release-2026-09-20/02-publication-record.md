# 阶段 18 发布记录

目标仓库：`https://github.com/NuoYe-CCnC/InkSight-GLHF`  
目标标签：`v0.1.0-test.1`  
Release 类型：pre-release

## 发布前状态

- 仓库为 Private，`main` 只有 1 个提交 `535211a5f95b757dfce413025febcebe1cf04a09`。
- 顶层只有候选 ZIP、校验文件和 5 个文档文件；完整源码尚未展开。
- 标签 0 个，Release 0 个。

## 本轮动作

- 以最终候选允许列表为唯一上传源，展开完整源码树。
- 更新 README、正式测试版说明、安全入口与 GitHub 发布清单。
- 重新生成候选 ZIP、SHA-256 和机器可读收据并执行全新解压复核。
- 上传源码、创建标签和 pre-release，启用私密漏洞报告，再将仓库设为 Public。

实际远程提交、Release URL、最终摘要和轻量验证结果在动作完成后记录于本机发布收据；不反向改写已发布 ZIP，避免摘要自引用。
