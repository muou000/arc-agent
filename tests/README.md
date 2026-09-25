# ARC-Bench Agent Tests

本目录是 ARC-Bench agent（arc-agent）的**成功测试套件**，用于锁定 ARC-Bench 平台
所依赖的公共契约。一旦后续改动破坏这些契约，对应测试会立即失败。

被保护的核心契约包括：

1. Python 包 [`arcbench_agent_runtime`](../arcbench_agent_runtime) 负责写出 runner 事件
   （`.arc/runner-events.jsonl`）并管理 traceability 数据（`.arc/traceability/*.json`）。
2. Web 模板 [`arc-template/templates/web-react-express/`](../arc-template/templates/web-react-express) 保持其运行契约（Express `/api/health`、Vite 构建、
   Vitest/Playwright 入口、`template.yaml` 清单与文件系统一致）。
3. 运行结束后的 **auto TDD re-prompt** 纯函数（`core/tdd_retry.py`）：扫描 runner 事件中的
   `test/failed` 节点并构造 TDD 优先的修复提示。

---

## 目录结构

```
tests/
├── conftest.py                  # 共享 fixture（tmp_project_dir / clean_env / runtime）
├── helpers/                     # 共享测试基建（被 agent / workflow / SDK 测试导入）
│   ├── faux.py                  #   faux 模型 + 假 app handler：驱动真实 agent 循环，无真实 LLM
│   └── jsonl.py                 #   .arc/ 产物 JSONL 读取助手
├── test_python_sdk/             # SDK 契约：arcbench_agent_runtime 的事件、追溯表、路径与 Git
├── test_model/                  # 模型适配器契约：重试、流式回退、客户端缓存、成本与 usage
├── test_agents/                 # agent 层：阶段提示词、纪律与工具边界、faux harness e2e
├── test_workflow/               # 编译工作流：队列调度、worktree 并行、阶段门禁、TDD 执行
├── test_app_type_handler/       # app-type handler：注册表、依赖安装、构建/测试执行与门禁
├── test_template_contract/      # 模板契约：清单 <-> 文件系统（快）+ npm 安装/构建（slow）
├── test_evals/                  # A/B 评测：指标聚合、对比数学、报告渲染（假 runner，无模型访问）
└── test_*.py                    # 顶层横切单测：需求校验、路径安全、可视化分析等（见下文）
```

目录即测试分层，按被测面自底向上排列：SDK 与模型适配是底座，agent 层与编译工作流
在其上，app-type handler 与模板契约锁定生成产物的运行时面，评测把整个编译当黑盒。
`conftest.py` 与 `helpers/` 是共享基建而非测试；未归入子目录的横切单测直接放在顶层。

---

## 运行测试

在仓库根目录：

```bash
# 安装依赖（一次即可）
python -m pip install -r requirements.txt

# 快速路径：不需要 Node + npm 的全部测试
python -m pytest -p no:anyio -m "not slow"

# 完整套件（需要 Node.js + npm；部分用例需要网络执行 npm install）
python -m pytest -p no:anyio
```

`-p no:anyio` 用于禁用本环境中加载会失败的可选插件，与被测代码无关。

测试默认通过 `pytest-xdist` 多进程并行运行（`pytest.ini` 的 `addopts` 含 `-n8`）。
8 是实测拐点：全量快速套件在 16 核开发机上串行 6:21，`-n2`/`-n4` 约 3:13，
`-n8` 起 2:48，8 以上不再有收益（长尾重测试钉死 makespan）。需要回到单进程
串行时追加 `-n0`：

```bash
python -m pytest -p no:anyio -n0 -m "not slow"   # 串行运行快速路径
```

invalid escape 序列（如 `"\d"`、`"\/"`）由 `pytest.ini` 的 `filterwarnings =
error::SyntaxWarning` 全局拦截，测试模块在收集期直接报错，不再依赖逐模块
钉子测试；该门禁不覆盖 pytest 启动阶段（conftest 链顶层导入）的编译告警。

也可以使用 Make 目标：

```bash
make install     # pip install -r requirements.txt
make test        # = test-fast，无需 Node.js
make test-fast   # pytest -p no:anyio -m "not slow"
make test-slow   # 完整套件（需要 Node.js + npm）
make clean       # 清理 pytest 缓存与 __pycache__
```

### 标记

| 标记     | 含义                                                       |
| -------- | ---------------------------------------------------------- |
| `slow`   | 需要 `npm install`、启动 dev server 或拉起真实子进程树（如进程清理、后端启动）的测试。 |

默认**跳过** slow 测试。传入 `-m ""` 或 `-m slow` 可运行它们。

---

## 各层保护的内容

### `test_python_sdk/`

- `.arc/runner-events.jsonl` 事件结构（`requirement_state`、`runner_state`、`signal`、
  `interface_upsert`、`test_upsert`、`interface_status`）。
- `.arc/traceability/*.json` 全部 7 张表的结构（`requirements`、`scenarios`、`interfaces`、
  `tests`、`call_edges`、`node_states`、`node_contracts`）。
- `llm_usage` 事件的聚合计数（每节点/阶段/模型/Run 汇总）。
- `RuntimePaths` 的环境变量优先级（`ARCBENCH_OUTPUT_DIR` → `ARCBENCH_PROJECT_DIR` →
  `ARCBENCH_TEMPLATE_DIR`）。
- `GitClient` 身份解析、`.gitignore` 托管块，以及提交历史事件链。
- `AgentRuntime` 装配：events ↔ traceability 的 node-state 回调。

### `test_model/`

- 兼容 OpenAI 的模型适配器：API 重试与错误归一化（成功、瞬时失败、非重试错误、
  重试耗尽），客户端缓存按配置实例隔离。
- SSE 流式传输与 stream/retry 回退路径。
- 成本核算与 usage 捕获。

### `test_agents/`

- 阶段提示词与结构化响应契约：接口设计 / 测试生成 / TDD 指导提示词、契约字面量
  锚点、设计骨架修复与 payload 回退。
- 测试生成契约：`declare_test_manifest` 声明即锁、test 契约交接、E2E 场景隔离。
- 阶段纪律与工具边界：`stage_discipline` 拦截与出路、阶段能力表、模板共享面写入
  门禁、工具参数清洗与截断守卫、permission denied 提示、delete/glob 路径边界、
  write 回执指纹、glob read-deny 扣留命中披露。
- agent 基础设施：faux harness 本身、结构化输出支持探测、checkpointer 序列化、
  上下文缓存、会话复用、阶段会话接口、step 预算、skill 目录注入与选择、tool
  usage 事件、Windows 路径兼容。
- faux harness e2e：阶段循环、TDD 闭环、设计骨架修复全链路（`helpers/faux.py` 驱动
  真实 agent 循环，不访问真实模型）。

### `test_workflow/`

- `core/tdd_retry.py` 纯函数：`scan_test_failures` 读取 JSONL、去重、保持首次出现
  顺序、跳过空行/非 JSON/空 node_id；`build_tdd_reprompt` 在提示中包含 node_id、
  失败详情并完整表述 TDD 序列（先写失败测试，禁止为通过而弱化测试）；
  `collect_attempt_facts` 从 `tool_usage`/`llm_usage` 逐次事件流聚合前次尝试的
  客观数字（模型调用/只读调用/run_tests/成功写入，支持游标分段），工具名分类在
  `test_agents/test_stage_discipline.py` 与阶段纪律的写面对账。
- 队列与调度：并行调度规则与子树亲和分组、依赖门禁与 `BLOCKED_BY_DEPENDENCY`
  传播、任务崩溃标记节点失败、saved state 恢复、跨节点文件声明
  （`core/file_claims.py`）。
- worktree 并行与合并：`NodeWorktreeManager` 复用/隔离/合并语义、并行 drain、
  语义冲突仲裁。
- 阶段门禁：DESIGN 基线红门、接口硬门禁、DESIGN 门禁流水线化与契约漂移校验。
- TDD 测试执行器、IMPLEMENT 诊断产物清理、测试类型词汇表单一 owner。

### `test_app_type_handler/`

- app-type 注册表与 `AppTypeHandler` 基类契约（`TestRunResult` 结果载体）。
- Web 工作区门禁：workspace gates、端口清理、Node 版本门禁、后端模块语法检查。
- 依赖安装（含并行安装）与前端构建缓存。
- 测试执行：失败分类、测试路径安全、E2E 运行时会话与静态宿主可见性。
- 需求资产拷贝、脚手架上下文文件与 testing-library/dom peer 补装。

### `test_template_contract/`

- `template.yaml` 顶层键齐全，`agent_guidance` 声明的路径在磁盘上存在，声明的
  技术栈与 `package.json` 依赖一致、声明的测试框架齐全。
- 模板拷贝排除 `template.yaml`；
  `arc-template/templates/web-react-express/README.md` 中提到的每个 `npm run X`
  都在某个 `package.json` 中声明；运行时 helper 与 index 再导出、`.gitignore`
  模板存在。
- （slow）后端启动并对 `GET /api/health` 返回 `{"code":200,"message":"Backend Ready"}`。
- （slow）前端 Vite 构建产出 `frontend/dist/index.html`。

### `test_evals/`

- `core/evals.py` 的节点/task 状态分桶、run 级严格 pass 判定（runner 退出码为 0、queue task
  全部完成、所有节点为终态成功状态）与 `llm_usage` 聚合读取。
- 对比数学：按 repetition 配对、pass rate（pp）、tokens / latency / est. cost 均值差，
  latency distribution（mean/median/p95/min/max）、以及缺失遥测与未配对 repetition 时的
  unavailable 语义。
- 报告渲染与 pi `Eval Comparisons` 版式一致（`report.txt` / `report.json` / `runs.jsonl`）。
- 运行诊断：queue task 完成门禁、非终态节点拒绝、失败事件/fingerprint、traceability 测试汇总、
  LLM/tool 分维度聚合、`events_present`、arm 交替顺序和敏感环境变量名/值脱敏。
- `eval_table` 端到端（`fake_eval_runner.py`）：工件落盘、`.arc` 证据快照、工作区清理、
  超时与启动失败仍产出报告。
- `arc eval` 子命令的参数解析、校验与 `--runner-script` 注入路径。

### 顶层横切单测

未归入上述目录的顶层 `test_*.py` 覆盖横切小面：

- 需求与安全边界：`test_requirement_validation.py`（需求树有限、无歧义）、
  `test_clean_safety.py`（清理命令拒绝保护路径）。
- 可视化分析：`test_visual_precompute.py` 与
  `test_visual_analysis_{cache,concurrency,security}.py`（参考图分析预计算并行化、
  缓存按模型端点隔离、并发扇出、引用路径不越出需求资产目录）。
- 其他横切：`test_config_bootstrap.py`（环境加载与 cwd 无关）、`test_logging.py`、
  `test_exit_code_parsing.py`（测试运行器退出码解析）、
  `test_frontend_build_cache_env.py`（构建缓存键包含环境输入）、
  `test_template_database_contract.py`（生成工作区数据库引导的静态契约）。
- 测试环境隔离：`test_host_env_isolation.py`（conftest 隔离 fixture 的机械守卫）：
  调度开关名以 `core/scheduling_switches.py` 为单一权威表（conftest scrub 列表与
  core 读取点共用，注册即自动进隔离范围）；pytester 注入污染环境验证 autouse
  fixture 在位则内层绿、缺失则红（干净宿主/CI 上有牙，覆盖调度开关与 model env
  两组）；AST 对账 core 内未登记的 `ARC_*` 字面量与 core 外的调度开关重字面量。

---

## 编写新测试

- 使用 `tmp_project_dir`（空目录）与 `runtime` fixture，不要触碰宿主环境。
- 优先使用 SDK 而非自行实现 JSON I/O；SDK 辅助函数（`runtime.events.*`、
  `runtime.traceability.*`、`runtime.git.*`）正是 ARC-Bench 实际调用的接口。
- 需要 faux 模型 / 假 app handler 或读取 `.arc/` 产物 JSONL 时，使用
  `tests/helpers/`（`faux.py`、`jsonl.py`），不要再复制私有副本。
- 需要网络或 `npm install` 的用例必须打 `@pytest.mark.slow`，保证 `make test-fast`
  可离线快速通过。

## 故障排查

- **`ModuleNotFoundError: No module named 'yaml'`** — 执行 `python -m pip install pyyaml`。
- **`git executable not available`** — `gitops` 测试会自动跳过。
- **slow 测试期间 `npm error gyp`** — 后端需要 `sqlite3` 原生构建工具；失败时测试会优雅跳过。
