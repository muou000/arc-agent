# 配置参考

arc-agent 的全部配置通过环境变量表达，读取顺序为 `ARC_ENV_FILE`（缺省 `.env`，从项目根目录加载）→ 进程环境变量。运行 `python arc_main.py doctor` 可验证配置健康。

## 速查表

| 变量 | 默认值 | 一句话 |
|---|---|---|
| `OPENAI_API_KEY` | — | 模型推理 API Key（别名 `OPENAI_KEY`） |
| `OPENAI_BASE_URL` | — | API 基地址（别名 `OPENAI_API_BASE`） |
| `MODEL` | — | 主编码模型名 |
| `ARC_OPENAI_API_MODE` | — | `responses` 或 `chat_completions` |
| `VISUAL_API_KEY` / `VISUAL_MODEL` | — | 视觉模型（分析需求截图），两者需成对设置 |
| `ARC_ENV_FILE` | `.env` | 环境文件路径 |
| `ARC_DEBUG` | 关 | 调试日志 |
| `ARC_SKIP_BROWSER_INSTALL` | 关 | 跳过编译前检查的 Playwright 浏览器安装（无外网环境） |
| `ARC_AGENT_RECURSION_LIMIT` | `300` | 单个阶段 agent 会话最大步数（下限 20） |
| `ARC_MODEL_TIMEOUT` | `600` | 单次模型 API 请求超时秒数 |
| `ARC_MODEL_CONNECT_TIMEOUT` | `15` | TCP/TLS 连接建立超时秒数 |
| `ARC_MODEL_MAX_RETRIES` | `3` | 单次调用内原始尝试之外的重试次数 |
| `ARC_MODEL_RETRY_DELAY` | `5` | 重试间隔秒数 |
| `ARC_MODEL_RETRY_MAX_DELAY` | `60` | 重试间隔上限（服务端 Retry-After 优先） |
| `ARC_MODEL_MAX_CONSECUTIVE_FAILURES` | `5` | 跨调用连续失败熔断阈值（设 0 关闭） |
| `ARC_MODEL_STREAM_TRANSPORT` | `stream` | 流式传输策略（stream / retry / off） |
| `ARC_MODEL_STREAM_CHUNK_TIMEOUT` | `90` | 流式响应相邻 SSE chunk 最大间隔秒数（设 0 关闭看门狗） |
| `ARC_MODEL_STREAM_USAGE` | 开 | 流式请求携带 `stream_options.include_usage` |
| `ARC_STRUCTURED_OUTPUT` | `auto` | 结构化输出开关（auto / on / off） |
| `ARC_NODE_WORKTREES` | 开 | 每节点隔离 worktree 并行 |
| `ARC_MAX_CONCURRENT_TASKS` | `3` | 并行模式同时运行任务数（上限 8） |
| `ARC_AFFINITY_DEPTH` | `1` | 亲和分组切分深度 |
| `ARC_DESIGN_GATE_PIPELINE` | 关 | 依赖方 DESIGN 只等依赖 DESIGN 完成即放行 |
| `ARC_MERGE_ARBITRATION` | 关 | 合并层语义冲突 LLM 仲裁 |
| `ARC_REBASE_ON_MERGE` | 关 | 执行中任务触碰兄弟合并文件时按需 rebase 重放 |
| `ARC_VISUAL_PRECOMPUTE` | 开 | 编译前并发预分析需求参考图 |
| `ARC_VISUAL_PRECOMPUTE_CONCURRENCY` | `4` | 参考图预分析并发调用数 |
| `ARC_VISUAL_ANALYSIS_CONCURRENCY` | `4` | 需求截图并发分析上限（1-8） |
| `ARC_AUTO_TDD_RETRY` | 开 | 运行结束后自动构造 TDD 修复提示 |

## 模型连接

- `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MODEL`：主编码模型的连接三要素。设了 `OPENAI_KEY` 而未设 `OPENAI_API_KEY` 时自动拷贝（`OPENAI_API_BASE` 同理，见 `core/config.py` 的 `load_project_env`）。
- `ARC_OPENAI_API_MODE`：`responses`（OpenAI Responses API）或 `chat_completions`（chat.completions，兼容大多数网关）。
- `VISUAL_API_KEY` + `VISUAL_MODEL`：视觉模型凭据，用于分析需求参考截图；只设其一会在 doctor 检查中告警。

## 模型调用行为

超时、重试与熔断（`agents/model/openai_api_adapter.py`）：

- `ARC_MODEL_TIMEOUT`：单次请求的读超时。非流式请求的 read 超时等于完整生成时长——基准中 DESIGN 大调用实测可达 ~580s，故默认保持 600s。
- `ARC_MODEL_CONNECT_TIMEOUT`：连接建立超时；连接被静默丢弃时快速失败，不必等满读超时。
- `ARC_MODEL_MAX_RETRIES` / `ARC_MODEL_RETRY_DELAY` / `ARC_MODEL_RETRY_MAX_DELAY`：适配器重试循环。固定短延迟起步，服务端 `Retry-After` 头优先，间隔钳制在上限内。
- `ARC_MODEL_MAX_CONSECUTIVE_FAILURES`：跨调用熔断。同一端点连续 N 次模型调用失败后，后续调用立即失败并提示 `--resume`；任一成功即重置计数。设 0 关闭。

流式传输三开关：

- `ARC_MODEL_STREAM_TRANSPORT`：`stream`（默认，首次尝试即流式——SSE chunk 持续流动，可穿过网关对非流式响应的 ~120s 空闲切断；端点对流式请求回 4xx 时自动回退纯非流式并进程内记住该端点）/ `retry`（首次非流式，仅连接类失败后的重试切流式）/ `0/false/no/off`（完全关闭）。
- `ARC_MODEL_STREAM_CHUNK_TIMEOUT`：流式看门狗。流中途静默卡死（TCP 存活但零字节）在该时限内被发现并按连接类失败换传输方式重试，而不是等到读超时或把整个 agent 会话回退重放；有效值钳制到 `ARC_MODEL_TIMEOUT`。设 0 关闭。
- `ARC_MODEL_STREAM_USAGE`：开启后流式 chat.completions 请求携带 `include_usage`，末个 SSE chunk 携带端点真实 usage（含缓存命中），token 用量从 tiktoken 估算转为 reported 口径。某网关 4xx 拒绝该选项时设 0 恢复旧行为（端点会整体回退纯非流式——非流式响应自带 usage，计费不受影响，只失去流式对网关空闲切断的防护）。

结构化输出：

- `ARC_STRUCTURED_OUTPUT`：`auto`（默认，对自定义 `OPENAI_BASE_URL` 端点做一次进程内缓存的工具调用能力探测）/ `on`（强制启用）/ `off`（强制关闭）。auto 模式下还会独立探测 chat.completions 的 `response_format` json_schema（strict）支持（独立缓存、fail-open）：支持时 DESIGN 空接口修复轮会重建 agent 并在 `interfaces` 数组上携带 minItems 语义下界，把"合法交白卷"变成约束违规；不支持时静默走骨架填空修复路径。

## 并行调度

默认开启每节点 worktree 并行：每个运行中的任务在自己的 git worktree、独立 web 端口和独立 E2E 数据库中执行，阶段完成后合并回主工作区。三个旋钮：

- `ARC_NODE_WORKTREES`：总开关。设 `0/false/no/off` 恢复共享工作区的严格串行调度。
- `ARC_MAX_CONCURRENT_TASKS`：同时运行的任务数（仅并行模式生效，默认 3，钳制在 1-8）。
- `ARC_AFFINITY_DEPTH`：亲和分组切分深度。默认 1 = 顶层子树一组；设 2 起宽子树的深层子树各自成组并行（如 simple-keep 的 REQ-2），组内仍串行。
- `ARC_DESIGN_GATE_PIPELINE`：DESIGN 依赖门禁流水线化（默认关闭，设 `1/true/yes/on` 启用）。开启后依赖方 DESIGN 的等待条件从「依赖 IMPLEMENT 完成并合并」放宽为「依赖 DESIGN 完成并合并」，依赖方从依赖节点已登记的接口卡（带 `implemented` 标志，可区分已设计未落地的面）做增量设计；依赖方 IMPLEMENT 仍等依赖 IMPLEMENT 落地。开启后依赖 IMPLEMENT 合并时会对 DESIGN 写时登记的契约锚点（`file_path` + `first_line`）做漂移校验：实现偏离登记契约时记 `contract_drift` runner 事件并告警，启用 `ARC_MERGE_ARBITRATION` 且节点仲裁预算未花时升级仲裁修复一次，否则不阻塞、靠下游 TDD 红灯兜底。
- `ARC_MERGE_ARBITRATION`：合并层语义冲突 LLM 仲裁（默认关闭，设 `1/true/yes/on` 启用）。
- `ARC_REBASE_ON_MERGE`：合并落地不打断执行任务的按需重放（默认关闭，设 `1/true/yes/on` 启用，仅并行模式生效）。兄弟合并落地时只给执行中任务挂一条未应用合并（变更文件集）；执行中 agent 的文件工具调用（read/edit/write/delete）触碰该集合内路径时，middleware 在该工具边界静止点先 WIP commit（`wip:` 前缀提交工作树脏状态）再 rebase 到最新 integration HEAD，然后服务本次调用并在工具结果附变更清单。冲突以标记落文件、交执行 agent 用文件工具消解（允许反复；软护栏：同一阶段连续 3 次携带冲突的重放后停用该阶段剩余时间的 mid-phase 重放）；全程 fail-open——任何机械失败（含 Windows 残留 dev server 锁文件）静默跳过、恢复重放前状态、落回既有合并轨道（机械消解/仲裁/重排），不新增终态、重排与仲裁预算互不消费。git 机械操作全部系统侧（rebase 段持 #91 读写门的 reader），agent 无 shell、无 git。重放生命周期（replayed/conflicts/aborted）留痕 `rebase_replay` runner 事件。

行为概要：同一子树的任务在共享 worktree 目录中顺序执行（兄弟节点不竞争同一批骨架文件），不同子树并行，空闲槽位会从其他子树窃取任务；父子之间 DESIGN 串行，子节点从包含父壳层的 integration HEAD 出发做增量编辑；需求树声明的 `dependencies` 对 DESIGN 和 IMPLEMENT 都做门禁（默认下两阶段都等依赖 IMPLEMENT 落地；启用 `ARC_DESIGN_GATE_PIPELINE` 后 DESIGN 只等依赖 DESIGN），依赖节点失败时依赖方标记 `BLOCKED_BY_DEPENDENCY` 而非静默 PENDING；跨子树共享 glue 文件的纯追加冲突由合并层机械消解并受合并后健康检查门禁约束；启用 `ARC_MERGE_ARBITRATION` 时非追加冲突和健康门禁失败先升级给主模型仲裁一次（输入只含冲突文件三方内容与按冲突文件裁剪的双方契约卡，编辑仅限冲突文件集，产物须过健康门禁复验，每节点预算一次且两触发点共享，全程留痕 `merge_arbitration` runner 事件），仲裁失败或未启用的其余冲突按阶段各重排一次（DESIGN 冲突重排 DESIGN，IMPLEMENT 冲突只重排 IMPLEMENT、已完成的 DESIGN 产物保留；两阶段预算独立，重试从已合并的 integration HEAD 出发、冲突路径注入 prompt 绕行指引），同阶段二次冲突终判失败并保留该节点的 worktree 供排查。调度与合并的完整语义（含依赖环丢弃、亲和权重、文件占用注册）是维护者契约，见根目录 `AGENTS.md` 的「工作流和队列」。

## 视觉分析

- `ARC_VISUAL_PRECOMPUTE`：编译前并发预分析需求参考图（DESIGN 前置，避免逐节点串行等待），设 `0/false/no/off` 关闭。
- `ARC_VISUAL_PRECOMPUTE_CONCURRENCY`：预分析的并发调用数（默认 4）。
- `ARC_VISUAL_ANALYSIS_CONCURRENCY`：单节点 DESIGN 阶段截图分析的并发上限（1-8，默认 4）。
