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
| `ARC_NODE_WORKTREES` | 关 | 每节点隔离 worktree 并行（设 `1/true/yes/on` 启用） |
| `ARC_MAX_CONCURRENT_TASKS` | `3` | 并行模式同时运行任务数（上限 8） |
| `ARC_AFFINITY_DEPTH` | `1` | 亲和分组切分深度 |
| `ARC_DESIGN_GATE_PIPELINE` | 关 | 依赖方 DESIGN 只等依赖 DESIGN 完成即放行 |
| `ARC_STAGE_PIPELINE` | 关 | 节点级视觉就绪门禁与阶段任务流水线（实验性） |
| `ARC_MERGE_ARBITRATION` | 关 | 合并层语义冲突 LLM 仲裁 |
| `ARC_REBASE_ON_MERGE` | 关 | 兄弟合并落地后执行任务的下一文件工具边界即 rebase 重放 |
| `ARC_VISUAL_PRECOMPUTE` | 开 | 编译前并发预分析需求参考图 |
| `ARC_VISUAL_PRECOMPUTE_CONCURRENCY` | `4` | 参考图预分析并发调用数 |
| `ARC_VISUAL_ANALYSIS_CONCURRENCY` | `4` | 需求截图并发分析上限（1-8） |
| `ARC_AUTO_TDD_RETRY` | 开 | 运行结束后自动构造 TDD 修复提示 |
| `ARC_TDD_RETRY_FRESH_THREAD` | 关 | auto TDD retry 轮边界分叉新线程（线程 id 追加 `@retry{N}` 后缀，轮内会话仍互相续写） |

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

- `ARC_MODEL_STREAM_TRANSPORT`：`stream`（默认，首次尝试即流式——SSE chunk 持续流动，可穿过网关对非流式响应的 ~120s 空闲切断；端点对流式请求回 400/404/405/415/422 这类证明拒绝流式请求形状的状态码时，自动回退纯非流式并进程内记住该端点；认证（401/403）与瞬时（408/409/429）状态不算流式能力证据，走常规重试分类，不写该缓存）/ `retry`（首次非流式，仅连接类失败后的重试切流式）/ `0/false/no/off`（完全关闭）。
- `ARC_MODEL_STREAM_CHUNK_TIMEOUT`：流式看门狗。流中途静默卡死（TCP 存活但零字节）在该时限内被发现并按连接类失败换传输方式重试，而不是等到读超时或把整个 agent 会话回退重放；有效值钳制到 `ARC_MODEL_TIMEOUT`。设 0 关闭。
- `ARC_MODEL_STREAM_USAGE`：开启后流式 chat.completions 请求携带 `include_usage`，末个 SSE chunk 携带端点真实 usage（含缓存命中），token 用量从 tiktoken 估算转为 reported 口径。某网关 4xx 拒绝该选项时设 0 恢复旧行为（端点会整体回退纯非流式——非流式响应自带 usage，计费不受影响，只失去流式对网关空闲切断的防护）。

结构化输出：

- `ARC_STRUCTURED_OUTPUT`：`auto`（默认，对自定义 `OPENAI_BASE_URL` 端点做一次进程内缓存的工具调用能力探测）/ `on`（强制启用）/ `off`（强制关闭）。auto 模式下还会独立探测 chat.completions 的 `response_format` json_schema（strict）支持（独立缓存、fail-open）：支持时 DESIGN 空接口修复轮会重建 agent 并在 `interfaces` 数组上携带 minItems 语义下界，把"合法交白卷"变成约束违规；不支持时静默走骨架填空修复路径。

## 并行调度

默认共享工作区的严格串行调度；设 `ARC_NODE_WORKTREES=1/true/yes/on` 启用每节点 worktree 并行：每个运行中的任务在自己的 git worktree、独立 web 端口和独立 E2E 数据库中执行，阶段完成后合并回主工作区。并行模式的旋钮：

- `ARC_NODE_WORKTREES`：总开关。默认关闭；设 `1/true/yes/on` 启用每节点 worktree 并行。
- `ARC_MAX_CONCURRENT_TASKS`：同时运行的任务数（仅并行模式生效，默认 3，钳制在 1-8）。
- `ARC_AFFINITY_DEPTH`：亲和分组切分深度。默认 1 = 顶层子树一组；设 2 起宽子树的深层子树各自成组并行（如 simple-keep 的 REQ-2），组内仍串行。
- `ARC_DESIGN_GATE_PIPELINE`：DESIGN 依赖门禁流水线化（默认关闭，设 `1/true/yes/on` 启用）。开启后依赖方 DESIGN 的等待条件从「依赖 IMPLEMENT 完成并合并」放宽为「依赖 DESIGN 完成并合并」，依赖方从依赖节点已登记的接口卡（带 `implemented` 标志，可区分已设计未落地的面）做增量设计；依赖方 IMPLEMENT 仍等依赖 IMPLEMENT 落地。开启后依赖 IMPLEMENT 合并时会对 DESIGN 写时登记的契约锚点（`file_path` + `first_line`）做漂移校验：实现偏离登记契约时记 `contract_drift` runner 事件并告警，启用 `ARC_MERGE_ARBITRATION` 且节点仲裁预算未花时升级仲裁修复一次，否则不阻塞、靠下游 TDD 红灯兜底。
- `ARC_STAGE_PIPELINE`：节点级阶段流水线开关（默认关闭，设 `1/true/yes/on` 启用）。开启后视觉分析在协调器中按节点后台运行，正式 DESIGN 只等待自己的 `visual-ready` 结果；瞬时视觉错误按有限次数退避重试，终态失败只影响该节点及其后续阶段。关闭时保留原有编译前视觉预分析和串行阶段行为。
- `ARC_MERGE_ARBITRATION`：合并层语义冲突 LLM 仲裁（默认关闭，设 `1/true/yes/on` 启用）。
- `ARC_REBASE_ON_MERGE`：合并落地不打断执行任务的饿式重放（默认关闭，设 `1/true/yes/on` 启用，仅并行模式生效）。兄弟合并落地时给执行中任务挂未应用合并（变更文件集）；执行中 agent 的**下一次任意文件工具调用**（read/edit/write/append/delete，无论触碰哪个路径）在工具边界先 WIP commit（`wip:` 前缀提交工作树脏状态）再 rebase 到最新 integration HEAD（一次重放消费全部 pending，非文件工具不触发），然后服务本次调用并在工具结果附变更清单。冲突以标记落文件、交执行 agent 用文件工具消解，消解是**强制**的——标记未清期间冲突集之外的**写**被拒绝（读豁免：消解者可读任何文件以产出正确消解；冲突集自身可写，验证工具保持可用）；软护栏只计实际执行过 git 动作的冲突轮次，同一阶段 3 次后停用剩余时间的重放并 abort 悬挂中的 rebase。全程 fail-open——任何机械失败（含 Windows 残留 dev server 锁文件）静默跳过、恢复重放前状态、落回既有合并轨道（机械消解/仲裁/重排），不新增终态、重排与仲裁预算互不消费。git 机械操作全部系统侧（rebase 段持 #91 读写门的 reader），agent 无 shell、无 git。重放生命周期（started/resolved/conflicts/aborted）留痕 `rebase_replay` runner 事件。配套模板补丁把 Playwright 易变产物（test-results/、playwright-report/）加入 backend .gitignore，使其不进检查点、合并与重放。

行为概要：同一子树的任务在共享 worktree 目录中顺序执行（兄弟节点不竞争同一批骨架文件），不同子树并行，空闲槽位会从其他子树窃取任务；父子之间 DESIGN 串行，子节点从包含父壳层的 integration HEAD 出发做增量编辑；需求树声明的 `dependencies` 对 DESIGN 和 IMPLEMENT 都做门禁（默认下两阶段都等依赖 IMPLEMENT 落地；启用 `ARC_DESIGN_GATE_PIPELINE` 后 DESIGN 只等依赖 DESIGN），依赖节点失败时依赖方标记 `BLOCKED_BY_DEPENDENCY` 而非静默 PENDING；跨子树共享 glue 文件的纯追加冲突由合并层机械消解并受合并后健康检查门禁约束；启用 `ARC_MERGE_ARBITRATION` 时非追加冲突和健康门禁失败先升级给主模型仲裁一次（输入只含冲突文件三方内容与按冲突文件裁剪的双方契约卡，编辑仅限冲突文件集，产物须过健康门禁复验，每节点预算一次且两触发点共享，全程留痕 `merge_arbitration` runner 事件），仲裁失败或未启用的其余冲突按阶段各重排一次（DESIGN 冲突重排 DESIGN，IMPLEMENT 冲突只重排 IMPLEMENT、已完成的 DESIGN 产物保留；两阶段预算独立，重试从已合并的 integration HEAD 出发、冲突路径注入 prompt 绕行指引），同阶段二次冲突终判失败并保留该节点的 worktree 供排查。调度与合并的完整语义（含依赖环丢弃、亲和权重、文件占用注册）是维护者契约，见根目录 `AGENTS.md` 的「工作流和队列」。

## 视觉分析

- `ARC_VISUAL_PRECOMPUTE`：编译前并发预分析需求参考图（DESIGN 前置，避免逐节点串行等待），设 `0/false/no/off` 关闭。
- `ARC_VISUAL_PRECOMPUTE_CONCURRENCY`：预分析的并发调用数（默认 4）。
- `ARC_VISUAL_ANALYSIS_CONCURRENCY`：单节点 DESIGN 阶段截图分析的并发上限（1-8，默认 4）。

## 子进程环境白名单

生成应用的 build/test/install/npm 命令以子进程运行（`app_type_handler` 各执行点，环境统一由 `core/processes.py` 的 `build_subprocess_env` 构造）。这些命令执行的是 agent 可编辑的代码，**不继承完整宿主环境**——模型凭据（`OPENAI_API_KEY` 等 `.env` 内容）不会出现在子进程环境里，也就无法经 build/test 输出回流进模型上下文。

子进程只拿到三类变量：

- 工具链白名单：`PATH`/`HOME`/`USERPROFILE`/`TEMP`/`TMP`/`TMPDIR`、Windows 系统变量（`SystemRoot`/`SystemDrive`/`COMSPEC`/`windir`/`PATHEXT`/`APPDATA`/`LOCALAPPDATA`）、`NODE_ENV`、Python 编码（`PYTHONIOENCODING`/`PYTHONUTF8`）、Java/Android（`JAVA_HOME`/`JAVA_TOOL_OPTIONS`/`ANDROID_SDK_ROOT`/`ANDROID_HOME`/`GRADLE_USER_HOME`）、Playwright（`PLAYWRIGHT_BROWSERS_PATH`/`PLAYWRIGHT_DOWNLOAD_HOST`/`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD`/`PLAYWRIGHT_SKIP_BROWSER_VALIDATION`）、代理（`HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`，大小写两种拼写都会传递）。
- 显式枚举的 `ARC_*` 运行时契约键：`ARC_WEB_PORT`/`ARC_WEB_BASE_URL`/`ARC_DB_FILE`/`ARC_E2E_DB_LABEL`（生成代码实际读取的契约面；不做前缀通配，避免未来的凭据形状 `ARC_*` 变量被静默透传）。逐次计算的运行时值（`PORT`/`PLAYWRIGHT_BASE_URL`/E2E 数据库路径等）由调用方作为附加项显式层叠，不经宿主透传。
- 调用方显式附加项：如 web 运行时契约（`PORT`/`ARC_WEB_PORT`/`BASE_URL`/`VITE_API_BASE_URL`）与 E2E 数据库路径。

白名单漏传的失败模式是"构建失败、可诊断后按名补入白名单"；新增变量须在 `core/processes.py` 的 `_SUBPROCESS_ENV_ALLOWLIST` 登记并附理由。运行 arc 自身的子进程（编译入口、git 操作）不受此白名单约束。

