# 发行覆盖层与来源边界

日期：2026-09-14

## 两个互不混淆的对象

- 生产文件：`shared/backend/data/daily_messages.json`，仍是 12 条并带旧 `CC0-1.0` 标记。本阶段不修改它，也不依赖其许可成立。
- 发行输入：`docs/phase14-content-source-release-2026-09-14/daily-messages-replacement-draft.json`，现为四条发布者确认稿，许可字段为 `GPL-3.0-only`。

`shared/tools/release/build_candidate.py` 收集允许列表时，对候选路径 `shared/backend/data/daily_messages.json` 使用发行输入覆盖。覆盖发生在临时 staging 目录，不回写生产文件。候选清单、目录和 ZIP 记录的是覆盖后的四条文件哈希，因此从候选自身再次构建仍得到同一内容。

## 许可和来源口径

四条确认稿作为项目内容按 `GPL-3.0-only` 随候选分发。发布者陈述其主要由本项目 Codex／DeepSeek 任务生成，未另外引入第三方内容；文档保持“陈述”属性，不推导原创、不侵权或拥有全部权利。第三方代码、依赖、字体和服务条款继续以各自声明为准。

旧 12 条语料未被重新许可为 GPL，也没有被宣称已满足 CC0；解决方式是当前候选根本不分发它们。历史审计文档可保留当时结论，但均指向本阶段的后续状态，避免被误读为当前阻塞。
