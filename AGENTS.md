# arc-agent 开发准则

## 文档定位

本文件只约束 **arc-agent 本身的开发、测试、契约维护和仓库协作**。它不负责规定 arc-agent 生成应用时下游 agent 应该如何设计 UI、生成测试或实现业务。

生成应用时的行为以以下源码为准：

- `agents/context/prompts/`：各阶段 agent 的系统提示词和任务协议；
- `agents/runtime/stage_discipline.py`：工具调用、阶段边界和文件操作限制；
- `skills/`：全量目录注入各阶段 agent 系统提示词、由模型按需读取的生成阶段指导（认证/失败修复底线确定性注入，`agents/skills/selection.py`）。

如果修改了这些运行时规则，应更新对应测试；不要把下游 agent 的详细行为复制到本文件。

## 规则范围

- 根目录规则适用于 Python 编译器、agent 适配器、运行时 SDK、app-type handler、模板和测试。
- 修改 `skills/vercel-react-best-practices/` 或 `skills/vercel-composition-patterns/` 时，遵守对应目录下的 `AGENTS.md`。
- 开发任务一律在独立的 git worktree 中进行（见「Git、依赖和敏感信息」）；开始前运行 `git status --short` 了解主工作区状态，只处理当前任务相关文件。

## 项目目标

arc-agent 是 ARC（Agentic Requirement Compiler）及 ARC-Bench agent 的实现。它将结构化需求树编译为应用工作区，并维护以下可审计链路：

- 需求节点、场景、依赖和父子关系；
- 接口契约、测试 manifest、测试状态和跨接口调用边；
- runner 事件流、节点状态、断点队列和 Git 检查点。

项目的工程目标是提高生成应用的真实可用性、评测通过率、Token 效率和完成时间。修改编译器时，优先保护端到端编译链路和 ARC-Bench 公共契约，而不是只优化某个局部函数。

## 代码布局

- `arc_main.py`、`main.py`：本地 CLI 和 ARC-Bench 平台入口。
- `core/`：编译队列、工作流、阶段调度、配置、日志、会话、失败重试和 A/B 评测（`core/evals.py`）。
- `agents/`：阶段 agent 适配器、上下文流水线、模型适配、运行时封装和工具。
- `app_type_handler/`：应用类型注册、模板初始化、依赖安装、构建和测试执行。
- `arcbench_agent_runtime/`：运行时路径、事件、追溯数据和 Git 操作的 Python SDK。
- `arc-template/templates/`：生成应用的模板。目前仓库内实际提供的模板布局应以目录内容为准。
- `skills/`：生成 agent 使用的阶段指导文件。
- `tests/`：SDK、agent、workflow、app-type 和模板契约测试。
- `records/`：诊断和运行证据；除非任务明确要求，不要重写历史记录。
- `.workbuddy-ai/`：工作区辅助记忆；不是产品源码，不要把它当作运行时契约来源。
- `pi/`：独立 TypeScript 项目，通常只存在于本地工作区、未纳入本仓库；若存在，按其目录内的 `AGENTS.md` 开发。

`.arc/` 通常是编译输出工作区中的运行时产物，包括 `processing_queue.json`、`node_sessions/`、`runner-events.jsonl` 和 `traceability/`；不要把手工修改 `.arc/` 当作修复源码的方式。

## 开发原则

1. **先定位所有者，再编辑。** 先阅读负责该行为的模块和现有测试，沿着调用链定位真正的 owner；避免在入口文件中堆叠业务逻辑或复制状态。
2. **保持契约一致。** 修改事件字段、追溯表、manifest、队列状态、模板清单、测试路径或 CLI 参数时，同时检查写入方、读取方、适配器、文档和契约测试。
3. **优先端到端正确性。** 对工作流、app-type、模板或测试执行器的修改，要确认“需求输入 -> agent 阶段 -> 工作区 -> 构建/测试 -> 追溯输出”仍然连通。
4. **保持补丁局部。** 不做与任务无关的重构、格式化、依赖升级或目录迁移；抽象只用于消除真实重复或稳定已有边界。
5. **失败要分类。** 区分产品断言失败、测试生成错误、配置/模板错误、依赖缺失和模型 API 错误；不要通过硬编码结果、隐藏测试分支或伪造追溯数据让测试变绿。
6. **文档记录已确认的事实。** 将当前实现、路线图和未运行的验证门禁分开描述；不要把一次本地 smoke test 写成生产能力证明。

## 关键维护边界

### 工作流和队列

- 保持节点的设计、实施、成功、失败、恢复和重试状态可互相解释。
- `--resume` 应复用现有输出工作区和追溯产物；`--retry-failed` 与 `--retry` 只能在续跑语义下使用。
- 默认启用每节点 worktree 并行调度（设置 `ARC_NODE_WORKTREES=0/false/no/off` 可恢复共享工作区的严格串行调度）。并行下任务按子树亲和调度，切分深度由 `ARC_AFFINITY_DEPTH` 控制（默认 1=顶层子树一组；设 2 起宽子树的深层子树各自成组）：同一子树的任务在共享的 worktree 目录中顺序执行，不同子树并行（独立端口槽位和 E2E 数据库，默认并发 `ARC_MAX_CONCURRENT_TASKS=3`、上限 8），任务结束合并回主工作区；父子 DESIGN 串行——子节点的 DESIGN 等到父节点 DESIGN 完成并合并后才调度（失败的父节点不阻塞子节点），使子节点从包含父壳层的 integration HEAD 出发做增量编辑；需求树声明的 `dependencies` 对 DESIGN 和 IMPLEMENT 都做门禁——依赖节点的 DESIGN/IMPLEMENT 等到其依赖节点的 IMPLEMENT 结束（完成即已合并）后才开始，依赖方从依赖节点已落地的接口做增量设计而不是并行重复实现共享面；依赖 IMPLEMENT 失败时依赖方（声明依赖它的节点和等待其后代的祖先）被显式标记 `BLOCKED_BY_DEPENDENCY` 而非静默 PENDING，重试重置救活依赖节点后由释放逻辑（`_release_dependency_blocks`）对称解除；无法调度的依赖边（成环——含仅通过父子规则闭合的环、祖先与后代互指、外来队列引用未知节点）在队列构建/恢复时丢弃并告警；跨子树共享 glue 文件的纯追加冲突由合并层机械消解并受合并后健康检查门禁约束，其余冲突将该节点标记为失败并保留其 worktree。修改该模式时必须同步维护 `core/worktree.py` 的复用/隔离区/合并/冲突语义、`_next_affinity_task` 的分组规则、端口槽位分配和 `_task_dependencies_met` 的顺序规则，并配套真实 git 的回归测试。
- 修改 post-run TDD retry 时，保持 `.arc/runner-events.jsonl` 的扫描字段、去重顺序和失败上下文传递一致。

### Runtime SDK 和追溯数据

- 使用 `arcbench_agent_runtime` 的公开 API 写事件、追溯数据和 Git 操作，不在调用方复制 JSONL/JSON 持久化逻辑。
- 七张追溯表和 runner 事件是对外可观察契约：`requirements`、`scenarios`、`interfaces`、`tests`、`call_edges`、`node_states`、`node_contracts`。
- 变更记录结构时，至少同步更新 dataclass/写入逻辑、读取逻辑、适配器和 `tests/test_python_sdk/`。
- 路径解析必须保留明确的项目目录边界，避免把宿主绝对路径、虚拟 `/workspace/` 路径和生成工作区相对路径混用。

### Agent、模型和提示词

- `agents/context/prompts/`、`agents/runtime/`、`agents/skills/` 是生成行为的源码，不要只修改 README 或根 `AGENTS.md` 来改变 agent 行为。
- 修改阶段提示词、结构化响应、工具权限、checkpointer、上下文缓存或 stream/fallback 逻辑时，优先使用 `tests/test_agents/` 中的 faux harness 和隔离 fixture。
- 模型适配器的 API mode、重试、错误归一化和客户端缓存属于独立契约；修改时覆盖成功、瞬时失败、非重试错误、重试耗尽和不同配置实例等情况。
- 普通单元测试不得调用真实模型或消耗付费 token。需要 provider 手工验证时，明确标记为手工/集成验证，不要把凭据写入测试或日志。

### App-type handler 和模板

- `app_type_handler` 的注册表、模板目录、`template.yaml`、package manifest、测试路径和运行命令必须保持一致。模板以 `ARC_AGENT_TEMPLATES_ROOT`（平台烤入的官方模板）为权威来源，未设置时使用仓库内 `arc-template/templates/` 副本（内容与官方模板保持一致，有意偏离需说明理由并同步测试）；运行时契约（端口来源环境变量、Playwright 浏览器安装、前端测试 peer 依赖）必须与官方模板兼容，不得假设副本特有的脚本或依赖。
- 仓库内模板只镜像官方模板，不承载修复：平台烤入的模板不会被仓库副本的改动影响，因此必须随模板到达每个生成工作区的修复要写成 `app_type_handler/template_patches.py` 的定向补丁，并在拷贝模板后（`WebAppType.post_template_setup`）应用到工作区。补丁必须标记守卫——目标已含修复时空操作，形状不认识时保留原文件并告警，不做整文件覆盖或半途写入；改动时同步 `tests/test_template_database_contract.py`、`tests/test_template_contract/` 和 `tests/test_app_type_handler/test_workspace_gates.py`。
- 修改 Web 模板时，检查前端构建、后端健康端点、单端口运行、数据库测试隔离和 Playwright/Vitest 契约；修改 Android/CLI handler 时，确认对应模板和环境门禁真实存在，不要只增加注册表项。
- 模板契约变更必须同步更新 `tests/test_template_contract/` 和模板 README。依赖或 lockfile 变更应说明原因，并确认不是无关版本漂移。

### A/B 评测

- `core/evals.py` 的五指标口径（pass rate、tokens、cache hit rate、latency、est. cost）、`runs.jsonl` / `report.json` 字段和报告版式是评测工作流契约；修改时同步 `tests/test_evals/` 与 `docs/evals.md`（README 只保留命令示例与指路）。
- 评测通过子进程调用 compile（缺省 runner 为仓库 `arc_main.py` 的 `compile` 入口；`--runner-script` 注入的通用脚本只接收 `<requirement> -o <workspace> -t <type> --port <port> [arm 参数]` 纯运行参数），只读取运行工作区的 `.arc/` 产物（runner 事件、队列）聚合指标；不要为取指标绕过 runtime SDK 直接改写工作区。测试一律使用注入的 runner（如 `tests/test_evals/fake_eval_runner.py`），不得消耗真实模型调用。报告默认写入 `records/evals/`，属于新增运行证据，不要改动已有评测目录。

## 测试和验证

从仓库根目录运行：

```text
# 快速套件：不运行 slow 测试，通常不需要 Node.js
python -m pytest -p no:anyio -m "not slow"

# 完整套件：包含模板集成测试，可能需要 Node.js/npm 和网络
python -m pytest -p no:anyio

# 配置健康检查：需要本地环境变量或 .env
python arc_main.py doctor
```

测试分层和新增测试规范见 [`tests/README.md`](tests/README.md)。一般要求：

- 修改代码后运行受影响的定向测试，再运行快速套件；
- 修改模板、app-type、构建/测试执行器或依赖安装逻辑时，补跑相关 slow 测试；
- 需要临时项目、Git 或 runtime 的测试使用现有 `tmp_project_dir`、`runtime` 和 faux fixture，不触碰宿主项目；
- 普通单测不得因宿主环境变量（如存在 `OPENAI_API_KEY` 等凭据或真实 endpoint）而改变行为或自动激活集成路径；测试激活范围只由显式标记和 fixture 决定，需要真实凭据的验证按手工/集成验证处理；
- slow 测试因环境不能运行时，报告为“未运行”，不要把 skip 当作通过；
- 测试失败时保留完整失败原因，先修复隔离、契约或实现问题，再调整断言。

`make test` 和 `make test-slow` 是上述测试的快捷入口；在没有可用 `make` 的环境中直接使用 Python 命令。

## Git、依赖和敏感信息

- 开发任务一律在独立的 git worktree 中进行，无论是否存在并发会话；不要直接在 `main` 的共享工作区改动或新建/切换 branch（branch 切换会改变工作区内容，可能破坏其他会话或用户正在进行的工作）。**这同样覆盖会话产物文件——ADR（`docs/adr/`）、领域词汇表（`CONTEXT.md`）、设计文档、诊断笔记等一律先落任务 worktree 再随任务分支提交**，严禁直接写入 `main` 共享工作区（哪怕只是"先记下来"——留在 main 工作区的未跟踪文件会被后续任务误认、误提交或过期）。每个任务：

  ```text
  git worktree add ../<任务名> -b <任务分支名>
  cd ../<任务名>
  ```

  任务完成后把该任务分支 push 到上游并发起 PR（不要在本地直接合并到 `main`）；PR 合并后再用 `git worktree remove ../<任务名>` 清理 worktree。
- git worktree 使用注意事项：

  - worktree 共享同一套仓库对象和分支，但每个 worktree 的工作区文件和索引相互独立；同一分支同时只能被一个 worktree 检出，因此创建 worktree 时应一并 `-b` 新建任务分支，避免检出冲突。
  - 操作前先用 `git worktree list` 查看现有 worktree；不要移动、删除、checkout 或重置其他会话正在使用的 worktree，清理前确认对应分支已合并。
  - worktree 之间不共享未跟踪文件。
  - 测试、构建和生成的应用只在自己的 worktree 内执行；多个 worktree 并行运行时注意端口和临时目录等资源的冲突。
  - 这里的开发用 worktree 与 `ARC_NODE_WORKTREES`（生成应用阶段的节点级任务 worktree，见「工作流和队列」）是两个不同的机制，互不影响。
- 不执行会覆盖用户工作的 `git reset --hard`、`git checkout`、`git clean` 或无范围的 `git add -A`；除非用户明确要求，不提交代码。
- 需要提交时才提交：提交只进入本任务 worktree 创建时用 `-b` 新建的任务分支，不要提交或推送到 `main`；任务分支通过 push 到上游并创建 PR 合并回 `main`，不要在本地直接合并。只提交本任务修改的代码内容：本任务独占的文件按显式路径 `git add`；同一文件混有其他任务或用户的修改时，用 `git add -p` 只暂存本任务的改动块；提交前用 `git diff --staged` 核对暂存内容不含其他会话的修改。
- 不把 API key、完整 `.env`、真实用户数据、生成工作区敏感文件或 provider 响应凭据写入日志、测试、提交或文档。
- `package-lock.json`、依赖版本、模板入口、事件字段和追溯表结构都是受保护接口。必要修改可以进行，但必须有明确理由、对应测试和同步文档。
- 只在任务需要时修改 `records/` 或 `.workbuddy-ai/`；它们不能替代代码、测试和可复现的验证证据。

## 文档同步

面向使用者的文档分两层：README 只保留简介、快速开始和指路链接；环境变量细节的权威位置是 `docs/configuration.md`，评测与用量观测细节的权威位置是 `docs/evals.md`。以下变化需要同步检查对应文档、测试说明和本文件是否仍准确：

- CLI 参数、环境变量或安装方式变化（同步 `docs/configuration.md`）；
- 目录布局、模板来源或 app-type 支持范围变化；
- 事件、追溯表、队列、测试 manifest 或输出产物变化（涉及用量/评测指标时同步 `docs/evals.md`）；
- 测试命令、slow 门禁或运行时前置条件变化。

根目录 `AGENTS.md` 应保持为维护 arc-agent 的短规则索引；生成应用的详细设计、测试和实现规则应继续留在运行时 prompt、middleware 和 skills 中。

## Agent skills

### Issue 跟踪

issue 以 GitHub Issues 形式跟踪（`muou000/arc-agent`，用 `gh` CLI 读写）。见 `docs/agents/issue-tracker.md`。

### Triage 标签

使用五个规范角色的默认标签字符串（`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`）。见 `docs/agents/triage-labels.md`。

### 领域文档

单上下文布局：根目录一个 `CONTEXT.md` + `docs/adr/`（均由 `/domain-modeling` 惰性创建）。见 `docs/agents/domain.md`。
