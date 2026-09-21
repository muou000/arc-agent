# 细化冲突域并以门禁流水线化换并行度，语义冲突交由合并层仲裁兜底

状态：**已落地**——杠杆①（亲和组深度切分，`ARC_AFFINITY_DEPTH`）已实现于 PR #79；杠杆③（合并仲裁，`ARC_MERGE_ARBITRATION`，`core/merge_arbitration.py` + `core/worktree.py` 仲裁钩子）已实现；杠杆②（DESIGN 门禁流水线化 + 契约漂移校验，`ARC_DESIGN_GATE_PIPELINE` 默认关闭 + `core/contract_drift.py`，前置 PR #64 登记地基）已实现。

PR #16 以顶层子树亲和分组 +「依赖 IMPLEMENT 完成才放行依赖方 DESIGN」的门禁压制了 run7 的并行重复实现（~23% design 冲突率），代价是宽扇出树约 80% 串行化（simple-keep：REQ-2 组独占 60/90 任务，任务单位模型下默认 ~74/90，且串行化大头在亲和分组——删光依赖字段也只能到 ~72）。决定：亲和组从顶层子树细化为可配深度的特性子树切分，依赖方 DESIGN 的门禁放宽为依赖 DESIGN 完成即可；由此重现的语义冲突不再靠串行化预防，改由合并层仲裁兜底（窄编辑权限于冲突文件集、预算 1 次、健康门禁复验），三道既有护栏（file claims / DESIGN 冲突一次性重排 / additive 消解 + 合并后健康检查）全部保留。全程 env 门控，默认行为不变。

## Considered Options

- 只改需求树：亲和分组不变时收益 ~3%（74→72 任务单位）；且 ARC-Bench 设定下需求为给定输入，不可作为机制。
- 依赖推断（从 call_edges/接口重写调度边）：错误双向不对称——漏推回到 run7 式重复实现，错推造成无谓串行——v1 排除。
- 每次合并例行 LLM 语义审查：token 按节点数线性付费；仲裁按冲突次数付费，可能为零。

## Consequences

- 放松门禁会暴露隐藏耦合（如 REQ-3.1 实际需要 REQ-2.7.3 的默认标签但只声明了父链），靠 IMPLEMENT 合并时的契约对照（PR #64 写时登记为地基）+ 仲裁发现，不再被过度串行顺带掩盖。
- PR #77（重试救活后 BLOCKED 解除）是硬前置：分组拆细后更多节点暴露在失败传播路径上。
- 需同步维护 `core/worktree.py` 复用/隔离区/合并语义、`_next_affinity_task` 分组规则、`_task_dependencies_met` 顺序规则与真实 git 回归测试（AGENTS.md「工作流和队列」）。
