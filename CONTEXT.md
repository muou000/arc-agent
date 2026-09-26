# arc-agent

arc-agent 是 ARC（Agentic Requirement Compiler）需求编译器的实现仓库：把结构化需求树编译为应用工作区，并维护可审计的链路（节点状态、接口契约、测试 manifest、runner 事件、Git 检查点）。本文件是仓库的领域词汇表。

## Language

### 调度与并行

**阶段流水线（Stage Pipeline）**：
在同一节点的 `VISUAL_ANALYSIS → INTERFACE_DESIGN → TEST_GENERATION → IMPLEMENTATION` 顺序不变的前提下，让写入集合不相交的相邻节点阶段在独立 worktree 中重叠执行；它只改变阶段执行和合并时机，不改变 requirements dependencies 或父子门禁。
_避免_：把阶段流水线与门禁流水线化混为一谈

**串行集成（Serial Integration）**：
阶段 agent 可以在隔离 worktree 中同时运行，但所有阶段发布物仍由单一 merge queue 按拓扑顺序串行合并；“串行”描述的是 integration，不是 agent 执行数。
_避免_：共享工作区并发写入

**亲和组（Affinity Group）**：
调度器把节点分进共享 worktree 的组，组内任务严格串行。默认按顶层子树划分；`ARC_AFFINITY_DEPTH` 配置切分深度，让宽子树的特性子树各自成组。
_避免_：worktree 组、并行组

**冲突域（Conflict Domain）**：
并行任务可能触碰的共享文件面的集合。亲和组是它的粗粒度代理；本设计将其细化为节点实际写的文件集合。
_避免_：共享面、耦合面

**门禁流水线化（Gate Pipelining）**：
把依赖方 DESIGN 的等待条件从“依赖 IMPLEMENT 完成并合并”放宽为“依赖 DESIGN 完成并合并”。
_避免_：设计提前、依赖放宽

**串行源（Serial Source）**：
任何把任务强制排队的调度约束。本仓库识别出三个：亲和分组、依赖门禁（DESIGN 等 IMPLEMENT）、并发上限。
_避免_：串行瓶颈、排队原因

### 依赖与耦合

**声明依赖（Declared Dependency）**：
requirements 中人写的 `dependencies` 字段。编译器契约：给定输入，不可修改。
_避免_：显式依赖

**隐藏耦合（Hidden Coupling）**：
需求文本暗示但未声明的依赖（如需要默认标签存在但只声明了父链）。过度串行会掩盖它们；放松调度会使其暴露。
_避免_：暗依赖、隐式依赖

**真实耦合（Real Coupling）**：
接口调用边（call_edges）体现的代码级依赖，与声明依赖独立存在，可能过度或不足。
_避免_：实际依赖

### 合并与仲裁

**阶段发布物（Stage Publication）**：
一个 stage worktree 完成后提交的不可变结果及其元数据，包括 `base_commit`、`artifact_commit`、声明的写入集合、契约哈希、测试 manifest 哈希和验证证据。发布物进入 merge queue 后才能改变 integration。
_避免_：阶段结果、临时分支结果

**写入集合（Write Set）**：
一个阶段声明可能新建、修改或删除的项目文件集合。写入集合用于阶段启动前的冲突域判定；集合相交时禁止阶段重叠，不把所有冲突推迟到 Git 合并。
_避免_：文件列表、修改范围

**机械消解（Mechanical Resolution）**：
合并层对“双方纯追加”冲突的自动消解（difflib 插入重放）。对语义重复失明——它正是让 run7 式双方代码共存的机制。
_避免_：自动合并、additive 消解

**仲裁（Arbitration）**：
机械层失败时（非追加冲突或合并后健康门禁失败）的 LLM 升级路径：拿三方 diff 与双方契约做裁决。编辑权窄限于冲突文件集，产出必须经健康门禁复验。
_避免_：merge agent、AI 审查

**文件占位（File Claim）**：
`core/file_claims.py` 的跨节点新建文件占位，防兄弟节点 add/add 冲突。
_避免_：文件锁、写锁

**未应用合并（Pending Merge）**：
兄弟任务合并落地时，仍在执行中的任务挂起的变更文件集。被该任务的一次饿式重放消费，或留到任务结束由合并层兜底。
_避免_：待同步、挂起合并

**饿式重放（Eager Rebase）**：
兄弟合并落地后，执行中任务在下一次任意文件工具调用的边界把自身分支重放到最新集成分支，与触碰的路径无关；一次重放消费全部未应用合并。
_避免_：按需重放、自动 rebase（这两个词都暗示别的触发语义）

**工具边界静止点（Tool-boundary Quiescence）**：
agent 串行循环中相邻两次工具调用之间的时刻，是该任务 worktree 唯一可被外部安全改写的点。
_避免_：中断点、暂停点

### 契约表述

**机械现实（Mechanical Reality）**：
运行时实际发生的行为——能力表裁决、工厂实际挂载的工具、中间件真实拦截与放行的调用。契约四份表述中唯一可被测试钉住的权威；表述层（文案、提示词、工具描述）漂移时，修复方向永远是表述跟随机械现实（ADR 0006）。
_避免_：以文案为准、以提示词为权威

**表述层（Restatement Layer）**：
对机械现实的全部复述：声明式能力表、拦截/状态文案、系统提示词、工具 description、技能文档。表述层与机械现实不一致叫漂移；漂移的验收标准是表述修正 + 钉子测试，而不是改变机械行为去迁就文案。
_避免_：文档层（暗示可有可无）、文案层

### 阶段边界

**视觉就绪（Visual Ready）**：
节点的视觉分析 stage 已成功持久化完整结果、缓存键和事件，因而可以进入 InterfaceDesigner。视觉就绪只释放本节点的视觉输入，不放宽父子或 declared dependency 门禁；失败节点进入可重试或终态失败。
_避免_：视觉预分析完成、图片已处理

**节点测试域（Node Test Domain）**：
一个稳定节点 ID 对应的测试路径命名空间和 manifest 所有权。TestGenerator 与 TDD 可以修改本节点测试域内的文件；兄弟节点不得共享测试文件，公共测试配置和 fixture 在模板初始化后只读。
_避免_：测试目录、测试分组

**骨架（Skeleton）**：
DESIGN 唯一可物化的文件形态，按形状判定而非长度：无函数体——只有 imports、类型/常量、带类型签名的导出、路由表到处理器名的映射、`// TODO(TDD): <行为>` 标记。
_避免_：脚手架代码、半成品实现、小型实现

**设计出路（Design Outlet）**：
装不进一次骨架写入的行为逻辑的唯一合法去处——写进阶段响应交给 TestDrivenDeveloper，而非拆块塞入文件。拒绝文案必须指向它；没有出路的护栏只制造返工。
_避免_：拆块续写、分块绕过

**回填阶梯（Backfill Ladder）**：
字段级校验判负之前的确定性补全顺序：条目自身字段 → 已存储的追溯行 → 机械推导（interface_id 的类型段、节点前缀机械 test_id、词汇表默认值）。全部来源穷尽才允许一次 repair，repair 穷尽才判负；没有回填来源且无法登记的条目丢弃但必须可观察。`coverage_scope` 缺失按 `owned` 处理——保守默认：错误地按 owned 处理会在基线 RED 门与 owned-覆盖检查中响亮失败，按 dependency/shared 默认则会静默豁免 witness（ADR 0007）。既有实现复用只有在接口状态、历史 implement checkpoint、完整覆盖映射和当前绿测结果同时成立时才记录为 `reused/converged`；任一证据缺失仍要求当前节点的 owned RED witness。
_避免_：校验即终判、静默丢弃

### 门禁与校验

**状态契约门禁（Status Contract Gate）**：
TestGenerator 产物中 HTTP 状态断言的静态契约校验：状态码按需求文本 → 接口卡 → 路由源码的顺序解析，无隐式 200 回退；契约提不出状态码时报 needs-info 诊断，断言与契约冲突时报 conflict 诊断。needs-info 的本意是把契约缺口交回设计侧补齐，不是拒绝与契约一致的断言。
_避免_：状态码校验器、HTTP 断言检查

**测试契约预检（Test Contract Preflight）**：
TDD session 启动前对生成测试的 fail-closed 静态检查（文件物化、模块语法、runner 入口与全局、runner 依赖、配置契约）。deterministic 诊断阻止 TDD 启动；只有运行时才能判定的问题（动态导入、bundler 别名）一律 fail-open 留给 runner。预检拦的是可静态证明的破损，真实 runner 仍是运行时权威——与机械现实原则同源（ADR 0006）。
_避免_：测试静态分析、TDD 门禁（泛称）

### 测试与评分

**仓库契约测试（Repository Contract Test）**：
仓库内验证 arc-agent 编译器、运行时和模板契约的测试；其通过不代表生成应用功能已被验收。
_避免_：内部测试（与生成 TDD 测试混淆）

**生成 TDD 测试（Generated TDD Test）**：
arc-agent 为生成应用编写并在开发过程中执行的测试；提供实现反馈，不是独立的评分证据。
_避免_：内部测试、正式验收

**独立代理验收（Independent Proxy Acceptance）**：
依据公开需求独立编写、从用户可观察行为检验生成应用的本地测试；不等于正式隐藏测试。
_避免_：外部测试（用例仍由开发侧掌握）

**冻结验收集（Sealed Acceptance Set）**：
独立代理验收中预先保留、不依据逐项结果持续调参的用例集合；并非正式隐藏测试。
_避免_：隐藏测试集

**正式隐藏测试（Official Hidden Test）**：
参赛者无法查看、由外部测评程序对最终交付应用执行并计入正式通过率的测试。
_避免_：生成 TDD 测试、独立代理验收
