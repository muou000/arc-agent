# ADR 0008: 串行集成下的阶段流水线与节点测试域隔离

日期：2026-09-24　|　状态：已接受；已实现（#251-#257），2026-09-25 起默认启用

## 背景

当前编译器有两处会限制串行模式的流水线重叠：

- `core/workflow.py` 在正式节点调度前执行全局视觉预分析；视觉调用本身已经有并发上限，但所有节点共享同一个前置屏障。
- `TestGenerator` 仍嵌在 `run_design_phase` 中，InterfaceDesigner、测试生成和 DESIGN 基线门在同一个 DESIGN 任务内完成，队列没有阶段级任务边界。

需求是保持 integration 的串行、可审计合并，同时让不共享写入面的阶段重叠：视觉分析提前运行；`TestGenerator(n)` 与 `InterfaceDesigner(n+1)` 重叠；节点测试生成与前一节点 TDD 可以在文件域隔离时重叠。

本决策不修改需求树的 dependencies、父子 DESIGN 门禁或 IMPLEMENT 门禁，也不把工作区内两个 agent 的并发写入视为安全。worktree 只隔离物理写入，契约、基线和运行时产物仍需要显式发布与合并规则。

## 决策

### 阶段模型

每个节点拆为四类可持久化的 stage task：

```text
VISUAL_ANALYSIS
      ↓
INTERFACE_DESIGN
      ↓
TEST_GENERATION
      ↓
IMPLEMENTATION / TDD
```

阶段状态为：

```text
VISUAL_ANALYSIS:  pending → running → ready | retry_wait | failed
其他阶段:         blocked → running → ready_to_merge → published | failed
```

`DESIGN` 继续作为 `INTERFACE_DESIGN + TEST_GENERATION` 的聚合状态，`IMPLEMENT` 继续作为 TDD 的聚合状态，以保持现有 CLI、队列读取方和追溯契约兼容。非叶子节点的 `TEST_GENERATION` 为 `skipped`，保留当前行为。

### 视觉就绪

视觉分析是后台 stage，不占用产品源码 worktree。编译开始后，对所有存在参考图的节点做有限并发的提前分析，并按图片、提示词、模型和端点输入去重缓存；正式节点仍必须等待自己的结果进入 `ready`。

视觉分析不绕过正式阶段的父子关系或 declared dependencies。它只提前准备输入，不释放 InterfaceDesigner、TestGenerator 或 TDD。分析结果、缓存和 runner 事件由协调器持久化，stage worktree 不直接写 `.arc`。

瞬时模型、网络或传输错误按有限重试和退避处理，确定性错误进入 `failed`，不无限重试、不静默降级。失败只阻塞当前节点的后继阶段；独立节点继续运行。编译结束时仍有必需节点失败，则编译失败。

### 流水线重叠

在各阶段写入集合不相交、共享测试资源只读且前置发布物已就绪时，允许：

- `TEST_GENERATION(n)` 与 `INTERFACE_DESIGN(n+1)` 重叠；
- `IMPLEMENTATION(n)` 与 `TEST_GENERATION(n+1)` 重叠。

TestGenerator 必须在 manifest、测试文件和 DESIGN RED baseline 完成后发布。TDD 只能修复当前节点测试域内的文件；下一个节点只能写自己的测试域。

现有父子和 declared dependency 门禁保持不变。阶段流水线不是 `ARC_DESIGN_GATE_PIPELINE` 的替代品，也不自动放宽依赖方 DESIGN。

### Stage worktree 与发布物

每个正式 stage 使用独立 worktree，从同节点前置阶段已经发布后的最新 integration HEAD 创建。stage 完成后不直接修改 integration，而是提交产品代码或节点测试文件，并返回不可变的阶段发布物。发布物至少包含：

- `base_commit`；
- `artifact_commit`；
- `declared_write_set`；
- `contract_hash`；
- `test_manifest_hash`；
- 阶段状态与验证证据。

阶段结果进入拓扑顺序的 merge queue。提前完成的阶段停留在 `ready_to_merge`，不能绕过同节点或依赖节点的前置发布。合并前检查写入集合和契约哈希；未预见的 Git 冲突允许一次 rebase 与健康门禁复验，仍失败则保留 worktree 并终止该阶段。第一版不新增语义合并仲裁。

阶段 worktree 不写 `.arc`、traceability 或共享队列 JSON。协调器在阶段发布和合并边界串行写入这些运行时产物，避免多个 worktree 合并 JSON/JSONL 造成伪冲突。

### 节点测试域

每个节点拥有稳定的测试路径命名空间，例如：

```text
tests/generated/<stable-node-id>/unit/...
tests/generated/<stable-node-id>/integration/...
tests/generated/<stable-node-id>/e2e/...
```

不要求一个测试一个文件，但 manifest 中的每个文件只能属于一个节点，retry/resume 时路径和身份必须稳定。公共测试配置、公共 fixture 和 runner 配置在模板初始化后只读；节点专属 fixture 放在节点测试域内。

阶段开始前必须登记写入集合。写入集合相交时不允许阶段重叠，而不是把冲突推迟到 Git 合并时。TestManifestLock 继续负责当前节点 manifest 的阶段内约束，并补充节点测试域和共享资源的边界检查。

### 兼容与启用

新增阶段流水线使用独立特性开关 `ARC_STAGE_PIPELINE`。实现落地时默认关闭、以显式 truthy 值启用；v1 实现完成后（2026-09-25）默认值翻转为启用，显式设置 `ARC_STAGE_PIPELINE=0/false/no/off` 恢复原有编译前视觉预分析和严格串行阶段路径。阶段流水线与每节点并行模式的组合仍未放开：并行模式下只有节点级视觉就绪门禁和写入域生效，阶段 worktree 与 merge queue 仅在串行调度下使用，组合门禁须先经过独立配置和真实 Git 测试。

## 非目标

- 不修改 requirements 的 dependencies 或通过代码调用边自动推断依赖。
- 不把视觉分析失败转换成无限等待。
- 不允许两个 agent 在同一个共享工作区同时写入。
- 不把单次视觉预分析或局部 smoke test 当作生产能力证明。

## 实现任务分解

1. **队列与状态**：扩展 `core/queue_state.py` 的 stage task 和持久化字段；保留 DESIGN/IMPLEMENT 聚合状态；补充恢复、重试和 `ready_to_merge` 测试。
2. **视觉就绪**：调整 `core/visual_analysis.py` 与 `core/workflow.py`，把全局屏障改为节点级 ready gate；实现去重、重试、失败传播和协调器持久化。
3. **阶段调度**：在 `core/scheduling.py` 与 workflow drain 中加入 stage 依赖、阶段槽位、背压和拓扑选择；不改变现有父子及 declared dependency 规则。
4. **阶段 worktree 与 merge queue**：扩展 `core/worktree.py` 或新增阶段管理器，记录 base/artifact/write-set 元数据，复用现有真实 Git 合并、冲突保留和健康门禁。
5. **Phase runner 拆分**：把 `core/phases.py` 中的 InterfaceDesigner、TestGenerator、RED baseline 和 TDD 边界拆成可独立发布的 stage；避免正式阶段重复调用已经完成的视觉分析。
6. **文件域与提示词**：同步 `agents/runtime/capabilities.py`、`agents/runtime/stage_discipline.py`、`agents/tools/test_manifest.py` 和阶段 prompt，强制节点测试域、共享资源只读和写入集合声明。
7. **运行时产物**：把阶段事件、traceability 和 queue state 的写入集中到协调器，确保 stage worktree 不产生需要 Git 合并的 `.arc` 文件。
8. **验证与 rollout**：增加 faux 模型、真实 Git、端口/数据库隔离、resume/retry、冲突、视觉失败和 TDD/TestGenerator 并发测试；用受控评测比较墙钟时间、pass rate、tokens、traceability 完整性和成本。

## 验收门禁

- 旧开关关闭时现有快速套件和完整套件行为不变。
- 阶段写入集合相交时不会并行，节点测试文件不会跨节点修改。
- `TestGenerator` 的 RED baseline、TDD 结果和 merge queue 状态可恢复、可审计。
- 视觉分析失败、阶段失败、合并冲突和重试都产生可解释的 runner 事件。
- 真实 Git worktree 合并后，七张追溯表和 `.arc` 产物不依赖 worktree 间的 JSON/JSONL 合并。
- 流水线模式在代表性需求树上降低墙钟时间，同时不降低 pass rate，不破坏依赖门禁和 resume/retry 语义。
