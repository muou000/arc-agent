# 评测与用量观测

本文档覆盖两部分：单次编译的用量/工具观测（`arc_main.py usage`），以及 A/B 评测（`arc_main.py eval`）。两者的数据都来自运行工作区 `.arc/` 产物（runner 事件、队列），指标口径互享。

## 单次编译的用量观测

### `llm_usage` 事件

每次模型调用的 token 用量与成本在编译过程中写入 `.arc/runner-events.jsonl`（`llm_usage` 事件，pi 风格语义：`input` 不含缓存读写，`reasoning` 是 `output` 的子集；provider 未返回 usage 时以 tiktoken 估算并标记 `source: estimated`）。每个事件还携带可选的 `latency` 块（`duration_s` 端到端耗时、`transport` 实际应答的传输方式 `streamed`/`plain`、`attempts` 适配器重试循环消耗的尝试次数；旧版本事件无该块，读取方需按可选处理）。

编译结束后聚合查看：

```bash
python arc_main.py usage --project-dir path/to/output          # 汇总报表
python arc_main.py usage --project-dir path/to/output --json   # 机读 JSON
```

报表维度：每节点 / 每阶段 / 每模型的用量、成本、调用延迟与传输分布，以及 provider 前缀缓存命中率。

### 缓存命中率口径

`cache_hit_rate = cache_read / prompt_tokens`，其中 `prompt_tokens` 是 provider 已报告 usage 的调用的 prompt 总量（`input + cache_read + cache_write`）；estimated 调用没有缓存分解，不计入分母，避免稀释命中率。provider 已报告但 cache 字段为 0 的调用视为真实未命中（不支持缓存的 provider 与从不命中的 provider 在数据上不可区分）；没有任何已报告调用的 bucket 命中率为 `null`（报表中显示 `-`，未测量），与真实 0% 区分。

按阶段（DESIGN / IMPLEMENT / TEST 等）的命中率视图用于定位前缀抖动：命中率低且 cache write 高，说明上下文前缀在漂移（时间戳、随机 ID、顺序变化），先修前缀稳定性——稳定内容在前、逐节点动态内容在后——这比压缩上下文更省成本。注意该指标度量的是 provider 的 prompt cache；`NodeContextCache`（`agents/context/pipeline.py`）只是进程内 memoize，省的是本地计算，与此指标无关。

### `tool_usage` 事件与浪费信号

agent 的每次工具往返（含被 stage discipline 拦截的调用）同样写入 `runner-events.jsonl`（`tool_usage` 事件：`tool` / `status`（`ok`、`error`、`blocked`）/ `detail`（文件路径、`read_file` 的 `offset`/`limit`、结果字符数与是否为空））。`usage` 命令聚合出每节点 / 每工具的往返次数，以及两个浪费信号：`unpaged_reads`（未带 `limit` 的整文件读取）和 `empty_results`（成功但返回为空，即无效 grep/读取），用于定位"全量读大文件""无效搜索"这类可修复的往返浪费。

### `layer_reverify` 事件与 TDD 迟到修复复验

某个测试层预算耗尽仍未通过、但判决时存在已全绿的后续层时（迟到落地的修复可能已让它变绿），TDD 阶段在判负前会系统自行复验一次：agent 会话结束后在预算计数之外重跑该层 manifest 内的全部测试文件，通过则该层按通过关闭，失败则照旧判负；存在未解决的环境失败时不触发。每次复验写入两条 `layer_reverify` 事件：`status` 取 `triggered`（触发留痕）与 `passed` / `failed`（结果留痕），字段含 `node_id` / `layer`（节点与层类型）、`files`（复验执行的 manifest 文件清单）、`used`（触发时该层已消耗的 `run_tests` 预算）和 `message`（`failed` 时的复验失败输出摘要，其余为 `null`）。`usage` 命令不聚合该事件；它会出现在评测报告 `events` 诊断的按类型事件计数中，用于解释"预算耗尽层最终按通过"的判定来源。

### `stray_sweep` 事件与游离重复文件清理

IMPLEMENT 阶段成功收尾时，系统会清理"游离重复文件"：路径在模板 `agent_guidance` 声明的全部骨架根之外、且内容与某个已提交的骨架内文件指纹相同（换行归一化后的 sha256，CRLF 副本视为同一文本）的文件会被删除——典型来源是 DESIGN 写错位置后又在正确位置重写的副本（如工作区根级 `src/api/auth.ts` 与 `frontend/src/api/auth.ts` 并存）。只满足单条件（内容重复但在骨架内 / 骨架外但内容不重复 / 孪生副本双方都在骨架外）的文件一律不动；不做通用"未引用文件"检测。删除写入一条 `stray_sweep` 事件（字段含 `node_id`、`files`（删除的相对路径清单）与 `message`（含每个文件与其骨架内孪生路径的对应关系）），并记入节点会话的 `swept_stray_files`。模板清单缺失或不可解析时清理静默跳过（fail-open）。`usage` 命令不聚合该事件；它会出现在评测报告 `events` 诊断的按类型事件计数中，用于解释工作区文件清单的收缩来源。

### `zero_test_leaf` 事件与零测试叶节点观测

叶节点声明了 owned 接口契约、但 TestGenerator 返回空 manifest 时，IMPLEMENT 会静默跳过 TDD 直接把接口标记为 implemented——空 manifest 是合法 DESIGN 结果（无本地行为的节点），但这与"漏检了 manifest"当前不可区分。为积累数据决定是否升级为门禁，该形态写入一条 `zero_test_leaf` 事件（字段含 `node_id`、`interface_count`（owned 接口数）与 `summary`（TestGenerator 响应自带的理由文本，未提供时为 `null`））。纯观测：不加门禁、不重试、不改变空 manifest 的合法性；无接口的叶节点与非叶节点不产生该事件。`usage` 命令不聚合该事件；它会出现在评测报告 `events` 诊断的按类型事件计数中，用于把零测试叶节点的分布与这些节点的评测通过率对照。

### 成本单价目录

内置单价取自基准评测模型目录（DeepSeek / Z.AI / Moonshot / MiniMax / Qwen，CNY 每百万 token，2026-09，见 `agents/model/costing.py`）。目录是封闭集合：模型名匹配不区分大小写（`MiniMax-M3` 与 `minimax-m3` 同价），表外模型一律不计成本（报表中显示为 unpriced），目录调整时直接更新 `costing.py` 中的 `_BUILTIN_MODEL_COSTS` 表。

## A/B 评测

`arc eval` 将同一份需求树分别以 baseline 和 candidate 两个配置各编译 N 次，输出五指标的 candidate − baseline 提升报告（移植自 ARC-Bench 参考实现 pi 的 `evalHarnessTable` 评测工作流）。两个 arm 的差异通过环境变量覆盖和附加 compile 参数表达：

```bash
python arc_main.py eval path/to/requirements \
  --baseline-env ARC_AUTO_TDD_RETRY=0 \
  --candidate-env ARC_AUTO_TDD_RETRY=1 \
  --repetitions 5
```

### 参数

- `--baseline-env` / `--candidate-env` 为各 arm 的环境变量覆盖（会盖过 `.env` 同名变量），`--baseline-arg` / `--candidate-arg` 为附加 compile 参数（追加在命令末尾，取值以 `=` 形式传入时可含 `--` 前缀）。每次运行都是干净子进程：runner 前缀之后由评测器统一追加 `<requirement> -o <workspace> -t <type> --port <port> <arm 参数>`，缺省 runner 是仓库 `arc_main.py` 的 `compile` 子命令；`--runner-script` 可换成任意脚本（接收上述纯运行参数，不带 `compile` 子命令）。
- 调试用 `--repetitions 1`，报告提升时建议 5 次；`--timeout` 给单次运行设置秒级上限，超时按失败运行计入报告。`--arm-order alternate` 会在 repetition 之间交替先运行 baseline/candidate，降低 provider 负载和缓存预热造成的时序偏差；默认仍保持 baseline-first 以兼容已有脚本。报告默认写入 `records/evals/<时间戳>-<名称>/`，`--out-dir` 可覆盖。
- 运行工作区默认放在系统临时目录并在快照后删除，`--work-root` / `--keep-workspaces` 可控制。

### 指标口径

运行按 repetition 配对：pass rate 是配对运行中编译成功（runner 退出码为 0 且无 FAILED 节点、queue task 全部完成且没有非终态节点）的占比，tokens / latency / est. cost 是配对运行的均值差。

```text
Eval Comparisons
  baseline vs candidate
     Baseline  baseline
    Candidate  candidate (5/5 pairs)
    Pass rate  +60.0 pp (candidate 80.0%, baseline 20.0%)
       Tokens  -1200.0 (candidate 22800.0, baseline 24000.0)
    Cache hit  +12.3 pp (candidate 61.5%, baseline 49.2%)
      Latency  -850.0ms (candidate 14000.0ms, baseline 14850.0ms)
    Est. cost  -¥0.0100 (candidate ¥0.1100, baseline ¥0.1200)
```

- `comparison.latency_distribution` 提供每个 arm 的 mean/median/p95/min/max 和 paired delta，避免长尾运行被均值掩盖；`report.txt` 的 p95 行同时标出样本数，避免把单次调试运行误读为稳定的百分位估计。
- cache hit rate 的口径与上文「缓存命中率口径」一致（`runs.jsonl` 中存 0–1 比率，报告中以百分点呈现），运行中没有任何 provider 已报告缓存分解的调用时记为 None。一侧缺失遥测时该指标标记 unavailable 而不是猜测。
- 成本为 CNY，来自 `agents/model/costing.py` 单价目录。

### 产物

报告目录包含 `report.txt` / `report.json`（结构化对比）、`runs.jsonl`（每次运行一条记录）和 `sessions/<run_id>/`（该次运行的 runner 事件、队列、追溯表、节点会话与控制台输出快照）。每条 run 还含 `diagnostics`：task 完成计数、失败事件/fingerprint、traceability 测试状态、LLM 按节点/阶段/模型聚合、tool blocked/error/empty/unpaged 信号和明确的 outcome 分类。`events_present` 标记 runner event 是否存在；缺失 event 不会伪造 token/cost 数据。工件中的 arm 环境变量会对敏感 key/token/secret/password 字段及常见 token 值脱敏。
