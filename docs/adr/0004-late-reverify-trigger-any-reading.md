# 迟到修复复验触发条件定案：后续任一层全绿即触发（issue #131）

状态：**定案，已实施**——issue #116 的受限复验通道（PR #126，已合入 main）采纳 **any** 读法；本文把 PR 评审指出的解释分歧显式留痕，行为与测试维持不变。

## 背景

issue #116 为 post-run TDD retry 的判负路径增加了受限复验通道，触发条件为「(a) 失败层为预算耗尽而非存在未修复失败，且 (b) 其后的层已全绿」。其中 (b)「其后的层已全绿」存在两种读法：

- **any**（PR #126 采纳）：后续**任一**层全绿，即触发该失败层的复验。
- **all**：**全部**后续层都全绿，才触发。

PR #126 选择 any 并在 `core/phases.py` 的 `_reverify_budget_exhausted_layer` docstring 中记录了理由；PR 评审（Spec 轴）指出这是对 #116 原文的解释分歧。本票把它变成显式决策，避免后续维护者从代码反推语义。

## 采纳 any 的理由

1. **触发证据是时间顺序，不是全覆盖。** 后续某层跑绿，即证明该失败层关闭之后仍有修复落地（后续层的会话在失败层关闭后继续编辑共享代码）——这正是复验通道要捕捉的信号。该证据由任何一层提供都成立，与中间层是否也绿无关。
2. **中间红层不构成阻塞，复验不可能掩盖失败。** 层与层独立判定：复验只跑失败层自己 manifest 内的测试文件、只跑一次、失败照旧判负（worktree 保留行为不变）。触发复验的最坏代价是**多一次 manifest 范围内的测试执行**，不会把任何红层洗绿。
3. **all 读法会复刻 #116 要修的浪费。** 「Unit 预算耗尽红、Integration 红、E2E 绿」场景下，all 读法放弃 Unit 的复验——尽管 E2E 全绿已证明迟到的修复真实落地。判负照旧发生，随之而来的是任务失败 + session 重启 + 三层重跑，正是 #116 的初始案例（easy-ticketbooking REQ-2）。
4. **误触发与漏触发代价不对称。** 复验是受限通道（一次、manifest 内、不通过判负），误触发代价有界（一次测试执行）；漏触发代价是整个任务单位的失败与重跑。不对称性支持把触发条件取宽。

## 翻转点

判定收敛在 `core/phases.py` `_reverify_budget_exhausted_layer` 内的单个谓词：

```python
if not any(executor.layer_passed(later) for later in ordered_types[successor_index:]):
    return
```

若未来翻转为 all（例如出现复验执行本身开销不可忽略的新证据），改动点就是这一处：`any` → `all`，并按 #131 验收标准补对照钉子测试——「中间层红、更后层绿」场景在 all 下不触发、在 any 下触发。

## 与现有钉子的关系

现有五个复验钉子（`tests/test_agents/test_tdd_loop_e2e.py`）在两种读法下行为一致，翻转 all 时需一并同步：

- `test_late_fix_reverify_passes_after_later_layer_green`、`test_late_fix_reverify_failure_keeps_layer_failed`：两层场景（Integration 耗尽红、E2E 绿），后续只有一层，any 与 all 无分歧。
- `test_no_reverify_when_no_later_layer_green`：后续层全红，两种读法都不触发。
- `test_no_reverify_while_environment_failure_unresolved`、`test_no_reverify_when_budget_not_exhausted`：通道级禁用条件（环境失败未修复、预算未耗尽），与读法无关。

any 与 all 的分歧场景（中间层红、更后层绿，如 Unit 耗尽红 + Integration 红 + E2E 绿）当前**没有**专门钉子——维持 any 的决定下不补（行为与测试不变）；该场景的对照钉子是翻转 all 时的一并交付物。

## 验证方式

文档性决策，无行为改动：复验钉子与快速套件随本 PR 原样运行通过，不新增测试。
