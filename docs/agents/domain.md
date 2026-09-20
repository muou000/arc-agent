# 领域文档

工程 skill 在探索代码库时应如何消费本仓库的领域文档。

## 探索前先读这些

- 仓库根目录的 **`CONTEXT.md`**；若根目录存在 **`CONTEXT-MAP.md`** 则以它为准：它指向每个 context 一个 `CONTEXT.md`，按主题读取相关的那份。
- **`docs/adr/`**：读取与你将要工作的区域相关的 ADR。多 context 仓库还要检查 `src/<context>/docs/adr/` 下的 context 级决策。

这些文件目前尚不存在。**不存在时静默继续**：不要标记它们的缺失，也不要预先建议创建。`/domain-modeling` skill（经 `/grill-with-docs` 和 `/improve-codebase-architecture` 到达）会在术语或决策真正落定时惰性创建它们。

## 文件结构

单 context 仓库（大多数仓库，本仓库即是）：

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-xxx.md
│   └── 0002-xxx.md
└── src/
```

多 context 仓库（根目录存在 `CONTEXT-MAP.md` 时）：

```
/
├── CONTEXT-MAP.md
├── docs/adr/                          ← 系统级决策
└── src/
    ├── ordering/
    │   ├── CONTEXT.md
    │   └── docs/adr/                  ← context 级决策
    └── billing/
        ├── CONTEXT.md
        └── docs/adr/
```

## 使用词汇表的词汇

当你的产出（issue 标题、重构提案、假设、测试名）命名领域概念时，使用 `CONTEXT.md` 定义的术语，不要漂移到词汇表明确回避的同义词。

如果所需概念还不在词汇表里，这是一个信号：要么你在发明项目不用的语言（请重新考虑），要么存在真实缺口（记录下来交给 `/domain-modeling`）。

注意：本仓库已有相当完整的领域词汇散布在根 `AGENTS.md`（需求树、DESIGN/IMPLEMENT、七张追溯表、manifest 锁定等）。在 `CONTEXT.md` 建立之前，以 `AGENTS.md` 的用词为准。

## 标记 ADR 冲突

如果你的产出与既有 ADR 矛盾，显式指出而不是默默覆盖：

> _与 ADR-0007（xxx）矛盾，但值得重开，因为……_
