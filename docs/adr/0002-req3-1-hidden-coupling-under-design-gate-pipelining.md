# REQ-3.1 隐藏耦合推演：DESIGN 门禁流水线化下的行为边界（issue #83）

依据：ADR 0001 Consequences 指出「放松门禁会暴露隐藏耦合（如 REQ-3.1 实际需要 REQ-2.7.3 的默认标签但只声明了父链），靠 IMPLEMENT 合并时的契约对照 + 仲裁发现，不再被过度串行顺带掩盖」。本文用 #80 fixture（`arc-bench-test/keep` 的 REQ-2 子树切片）逐条推演该节点在两种门禁模式下的调度与失败路径，并给出放宽/不放宽的边界结论。

## 事实

- REQ-3.1（Initial suggested filters）声明的依赖只有 `REQ-1.1`（进入网站）。
- 外部测试 `arc-bench-test/keep/tests/REQ-3.1.spec.ts` 断言三件事：聚焦搜索框 → 出现建议过滤器 → 点击 `FIXTURES.labels.default`（字面量 `Reminders`，helpers.ts:31）→ 断言提醒笔记可见（`Call dentist`）。
- `Reminders` 默认标签的存在由 REQ-2.7.3（Default label）提供；REQ-3.1 与 REQ-2.7.3 之间**没有**声明的依赖边（REQ-3.1 只声明 REQ-1.1；其父 REQ-3 声明依赖 REQ-2，但父链不传递给子节点的调度门禁——父依赖只门禁父节点自己的任务）。

## 默认模式（门禁关闭）下的行为

REQ-3.1 的 DESIGN 等 REQ-1.1 的 IMPLEMENT。但 REQ-3（父）的 DESIGN 等 REQ-2 的 IMPLEMENT——即整棵 REQ-2 子树（含 REQ-2.7.3）落地后，REQ-3.1 才开始设计。**隐藏耦合被父链的过度串行顺带掩盖**：等到 REQ-3.1 设计时，默认标签接口已在集成 HEAD 上，跨节点接口卡可读、`implemented=True`。

流水线化前的实际风险窗口：无。这正是 run8 串行语义的隐性收益，也是它掩盖问题的代价（宽树约 80% 串行化）。

## 流水线模式（`ARC_DESIGN_GATE_PIPELINE` 开启）下的行为

1. REQ-3.1 的 DESIGN 只等 REQ-1.1 的 DESIGN。此时 REQ-2.7.3 可能尚未设计（甚至 REQ-2.7 子树尚未开始）。
2. REQ-3.1 的 IMPLEMENT 仍等 REQ-1.1 的 IMPLEMENT，但与 REQ-2 子树**无先后保证**——REQ-2.7.3 的 IMPLEMENT 可能晚于 REQ-3.1 的 IMPLEMENT。
3. 若 REQ-3.1 的 TDD/实现先落地：它对建议过滤器列表的内容来源（默认标签 `Reminders`）只能从「需求文本 + 外部测试锚点」推断，或从接口卡读到 REQ-2.7.3 的契约（若其 DESIGN 已完成，卡上 `implemented=False` 可区分「已设计未落地」）。

## 边界结论（放宽/不放宽）

**这条耦合不应该靠加依赖边修复，也不应该阻止流水线化。** 理由：

- 需求树在 ARC-Bench 设定下是给定输入（ADR 0001 Considered Options 已排除改树）；REQ-3.1 只声明 REQ-1.1 是 authored data。
- 外部测试是最终裁判：REQ-3.1 若在 REQ-2.7.3 落地前实现完，其 E2E（点击 `Reminders` 过滤器）在**运行时**由外部评测执行，那时整棵树已交付——评测顺序里 REQ-2.7.3 必然已存在（同一交付物）。编译期的实现顺序不影响评测时的正确性，只影响 REQ-3.1 自己的 TDD 信号质量。
- 编译期内的护栏链已按层就位：
  1. **接口卡 `implemented` 标志**（main 已有）：REQ-3.1 设计时可区分依赖面的已设计/已落地状态，对 `implemented=False` 的契约按「设计中」处理（引用路径但避免断言运行时行为）；
  2. **契约漂移校验**（本票）：REQ-2.7.3 的 IMPLEMENT 合并时校验其登记锚点；若它漂移了 REQ-3.1 设计所依赖的形状，记 `contract_drift` 告警（仲裁开启时修复一次）；
  3. **下游 TDD 红灯**：REQ-3.1 自己的测试若依赖 REQ-2.7.3 的运行时状态而 REQ-2.7.3 尚未落地，红灯由 REQ-3.1 的 post-run TDD retry 或（若树已 drain 完）评测暴露——这是 ADR 0001 明确接受的兜底。

**不放宽的边界**：依赖方 IMPLEMENT 不放宽（本票实现保持 REQ-3.1 的 IMPLEMENT 等 REQ-1.1 的 IMPLEMENT），因为场景读运行时状态的缺失会被误报为测试失败。REQ-3.1 类隐藏耦合（未声明的跨子树读取）不属于任何门禁能静态发现的范畴——它正是「过度串行顺带掩盖」的确切含义，流水线化把它交给三层护栏处理是设计决定，不是回归。

## 验证方式

- 调度门禁与契约漂移行为：`tests/test_workflow/test_design_gate_pipelining.py` 中的 drain 级测试，覆盖流水线模式下依赖方 DESIGN 的可运行性以及 IMPLEMENT 合并前的契约校验。
- 漂移捕获：`tests/test_workflow/test_design_gate_pipelining.py`（drift 纯函数 8 测 + drain 级事件落盘/仲裁升级/预算耗尽 3 测）。
- 环图健康：`test_cycle_breaking_catches_the_pipelined_design_vertex_cycle`（checker 边传递覆盖流水线边的 soundness 论证随测试注释留档）。
