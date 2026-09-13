# arc-agent 开发准则

## 文档定位

本文件只约束 **arc-agent 本身的开发、测试、契约维护和仓库协作**。它不负责规定 arc-agent 生成应用时下游 agent 应该如何设计 UI、生成测试或实现业务。

生成应用时的行为以以下源码为准：

- `agents/context/prompts/`：各阶段 agent 的系统提示词和任务协议；
- `agents/runtime/stage_discipline.py`：工具调用、阶段边界和文件操作限制；
- `skills/`：按节点特征加载的生成阶段指导。

如果修改了这些运行时规则，应更新对应测试；不要把下游 agent 的详细行为复制到本文件。

## 规则范围

- 根目录规则适用于 Python 编译器、agent 适配器、运行时 SDK、app-type handler、模板和测试。
- 修改 `skills/vercel-react-best-practices/` 或 `skills/vercel-composition-patterns/` 时，遵守对应目录下的 `AGENTS.md`。
- 开始工作前运行 `git status --short`。保留其他会话或用户已有的修改，只处理当前任务相关文件。

## 项目目标

arc-agent 是 ARC（Agentic Requirement Compiler）及 ARC-Bench agent 的实现。它将结构化需求树编译为应用工作区，并维护以下可审计链路：

- 需求节点、场景、依赖和父子关系；
- 接口契约、测试 manifest、测试状态和跨接口调用边；
- runner 事件流、节点状态、断点队列和 Git 检查点。

项目的工程目标是提高生成应用的真实可用性、评测通过率、Token 效率和完成时间。修改编译器时，优先保护端到端编译链路和 ARC-Bench 公共契约，而不是只优化某个局部函数。

## 代码布局

- `arc_main.py`、`main.py`：本地 CLI 和 ARC-Bench 平台入口。
- `core/`：编译队列、工作流、阶段调度、配置、日志、会话和失败重试。
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
- 默认（`ARC_NODE_WORKTREES` 未开启）保持共享工作区的严格串行调度。`ARC_NODE_WORKTREES=1` 下每个任务在独立 git worktree、端口槽位和 E2E 数据库中运行，任务结束合并回主工作区；修改该模式时必须同步维护 `core/worktree.py` 的合并/冲突语义、端口槽位分配和 `_task_dependencies_met` 的顺序规则，并配套真实 git 的回归测试。
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

- `app_type_handler` 的注册表、模板目录、`template.yaml`、package manifest、测试路径和运行命令必须保持一致。
- 修改 Web 模板时，检查前端构建、后端健康端点、单端口运行、数据库测试隔离和 Playwright/Vitest 契约；修改 Android/CLI handler 时，确认对应模板和环境门禁真实存在，不要只增加注册表项。
- 模板契约变更必须同步更新 `tests/test_template_contract/` 和模板 README。依赖或 lockfile 变更应说明原因，并确认不是无关版本漂移。

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
- slow 测试因环境不能运行时，报告为“未运行”，不要把 skip 当作通过；
- 测试失败时保留完整失败原因，先修复隔离、契约或实现问题，再调整断言。

`make test` 和 `make test-slow` 是上述测试的快捷入口；在没有可用 `make` 的环境中直接使用 Python 命令。

## Git、依赖和敏感信息

- 每次执行独立任务前都必须新建专用 branch，并在该 branch 上完成修改；不要直接在 `main` 或其他共享 branch 上开发。
- 不执行会覆盖用户工作的 `git reset --hard`、`git checkout`、`git clean` 或无范围的 `git add -A`；除非用户明确要求，不提交代码。
- 不把 API key、完整 `.env`、真实用户数据、生成工作区敏感文件或 provider 响应凭据写入日志、测试、提交或文档。
- `package-lock.json`、依赖版本、模板入口、事件字段和追溯表结构都是受保护接口。必要修改可以进行，但必须有明确理由、对应测试和同步文档。
- 只在任务需要时修改 `records/` 或 `.workbuddy-ai/`；它们不能替代代码、测试和可复现的验证证据。

## 文档同步

以下变化需要同步检查 README、测试说明和本文件是否仍准确：

- CLI 参数、环境变量或安装方式变化；
- 目录布局、模板来源或 app-type 支持范围变化；
- 事件、追溯表、队列、测试 manifest 或输出产物变化；
- 测试命令、slow 门禁或运行时前置条件变化。

根目录 `AGENTS.md` 应保持为维护 arc-agent 的短规则索引；生成应用的详细设计、测试和实现规则应继续留在运行时 prompt、middleware 和 skills 中。
