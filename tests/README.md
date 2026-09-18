# ARC-Bench Agent Tests

本目录是 ARC-Bench agent（arc-agent）的**成功测试套件**，用于锁定 ARC-Bench 平台
所依赖的公共契约。一旦后续改动破坏这些契约，对应测试会立即失败。

被保护的契约：

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
├── test_python_sdk/             # 第 1 层：arcbench_agent_runtime SDK 单测
│   ├── test_context.py          # RuntimePaths
│   ├── test_jsonio.py           # jsonio 辅助函数
│   ├── test_events.py           # EventClient（含 llm_usage 事件契约）
│   ├── test_usage_aggregate.py  # llm_usage 聚合（每节点/阶段/模型/Run 汇总）
│   ├── test_traceability.py     # TraceabilityStore + 7 张表
│   ├── test_gitops.py           # GitClient（使用真实 git）
│   └── test_runtime.py          # AgentRuntime + 端到端流程
├── test_template_contract/      # 第 2 层：模板集成
│   ├── test_template_layout.py  # 清单 <-> 文件系统一致（无需 Node）
│   ├── test_template_backend.py # [slow] npm install + /api/health
│   └── test_template_frontend.py# [slow] npm install + vite build
└── test_workflow/               # 第 3 层：编译工作流纯函数
    └── test_post_run_tdd_retry.py  # core/tdd_retry.py 的扫描与提示构造
```

另有 `test_evals/`：A/B 评测工作流测试（`core/evals.py` 的指标提取、对比数学、报告渲染，
`eval_table` 端到端与 `arc eval` CLI，全部基于 `fake_eval_runner.py` 假 runner，无模型访问）。

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
| `slow`   | 需要 `npm install` 或启动 dev server 的测试。               |

默认**跳过** slow 测试。传入 `-m ""` 或 `-m slow` 可运行它们。

---

## 各层保护的内容

### `test_python_sdk/`

- `.arc/runner-events.jsonl` 事件结构（`requirement_state`、`runner_state`、`signal`、
  `interface_upsert`、`test_upsert`、`interface_status`）。
- `.arc/traceability/*.json` 全部 7 张表的结构（`requirements`、`scenarios`、`interfaces`、
  `tests`、`call_edges`、`node_states`、`node_contracts`）。
- `RuntimePaths` 的环境变量优先级（`ARCBENCH_OUTPUT_DIR` → `ARCBENCH_PROJECT_DIR` →
  `ARCBENCH_TEMPLATE_DIR`）。
- `GitClient` 身份解析、`.gitignore` 托管块，以及提交历史事件链。
- `AgentRuntime` 装配：events ↔ traceability 的 node-state 回调。

### `test_template_contract/`

- `template.yaml` 中 `agent_guidance` 声明的路径在磁盘上存在。
- `template.yaml` 声明的技术栈与 `package.json` 依赖一致。
- `arc-template/templates/web-react-express/README.md` 中提到的每个 `npm run X` 都在某个 `package.json` 中声明。
- （slow）后端启动并对 `GET /api/health` 返回 `{"code":200,"message":"Backend Ready"}`。
- （slow）前端 Vite 构建产出 `frontend/dist/index.html`。

### `test_workflow/`

- `core/tdd_retry.py` 的 `scan_test_failures`：读取 JSONL、去重、保持首次出现顺序、
  跳过空行/非 JSON/空 node_id。
- `core/tdd_retry.py` 的 `build_tdd_reprompt`：在提示中包含 node_id、失败详情，并完整
  表述 TDD 序列（先写失败测试，禁止为通过而弱化测试）。

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

---

## 编写新测试

- 使用 `tmp_project_dir`（空目录）与 `runtime` fixture，不要触碰宿主环境。
- 优先使用 SDK 而非自行实现 JSON I/O；SDK 辅助函数（`runtime.events.*`、
  `runtime.traceability.*`、`runtime.git.*`）正是 ARC-Bench 实际调用的接口。
- 需要网络或 `npm install` 的用例必须打 `@pytest.mark.slow`，保证 `make test-fast`
  可离线快速通过。

## 故障排查

- **`ModuleNotFoundError: No module named 'yaml'`** — 执行 `python -m pip install pyyaml`。
- **`git executable not available`** — `gitops` 测试会自动跳过。
- **slow 测试期间 `npm error gyp`** — 后端需要 `sqlite3` 原生构建工具；失败时测试会优雅跳过。
