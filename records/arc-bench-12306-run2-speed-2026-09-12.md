# 用 ARC-Bench「12306」压测 arc-agent —— 实测运行 + 架构提速分析

日期：2026-09-12（下午第二轮）
被测：`D:\code\arc-agent`（main @ `93b3468`）
输入：`D:\code\arc-bench\arc-bench\webapp\12306\requirements`（143 节点 / 117 叶 / 286 个任务）
输出：`C:\Users\25137\AppData\Local\Temp\arc-run-12306`（快盘，规避已知 D 盘元数据税）
命令：

```bash
export CODEBUDDY_SAFE_DELETE_ENABLED=0
python arc_main.py compile <12306/requirements> -o <C:\...\Temp\arc-run-12306> -t web --port 3301 --clean
```

---

## 0. 结论摘要

**1）链路是通的，产物是真的能跑的。** 之前修掉的三项 P0（模板补齐、npm 健壮化、前置门禁）在本次运行中全部生效：
工作区门禁 45 秒走完，编译正常进入节点循环；首个叶子节点 REQ-1.1 生成的 React 组件 + 前端集成测试 + Playwright E2E
全部落盘，**E2E 实测 `1 passed (3.5s)`**；独立启动生成的应用，`/api/health` 返回 `{"code":200,"message":"Backend Ready"}`，
首页正常输出 HTML。

**2）但速度仍然不成立：全量跑完需要约 27 小时。** 29 分钟只推进了 286 个任务里的 5 个（1.7%），
线性外推 = **约 27.4 小时**。这个量级下任何"跑完整套 12306 基准"的尝试都不现实。

**3）慢的原因不是模型慢，是"重复劳动 + 空转"。** 模型等待只占 53%，而**工具调用里 68% 是重复探索**、
**run_tests 里 72% 是无效调用**（预算耗尽 / 层不匹配的拒绝）、**同一批绿灯测试被重复跑了 6 次**。
这些全部是架构层可以消掉的成本，不是模型能力问题。

按下面的 P0 三项 + P1 三项落地，本用例的墙钟时间预期可以压到 **1/4 以下**（详见 §4 收益估算）。

---

## 1. 本次运行实测数据

### 1.1 总览

| 指标 | 数值 |
| --- | --- |
| 墙钟时间 | **28.7 min**（15:00:32 → 15:29:15，1723 s） |
| 队列 | 286 任务，完成 **5**（1.7%），PENDING 280，RUNNING 1 |
| 推进的节点 | ROOT（非叶，跳过）、REQ-1（DESIGN）、REQ-1.1（DESIGN+IMPLEMENT）、REQ-1.2（DESIGN） |
| 外推全量 | **≈ 27.4 小时** |
| 模型轮次 | 158（TDDev 101 / InterfaceDesigner 37 / TestGenerator 20） |
| 工具调用 | ≈ 410（去重后） |

### 1.2 墙钟时间构成（拆自 debug.log 时间戳）

| 分类 | 耗时 | 占比 | 说明 |
| --- | --- | --- | --- |
| 模型等待 | **885 s** | **53.3%** | 工具结果 → 下一次模型响应 |
| 工具执行 | 243 s | 14.7% | 含测试运行（单次 E2E 25–50 s） |
| 其它 | 531 s | 32.0% | agent graph 步进、上下文拼装、日志落盘 |

模型单轮延迟：均值 **6.5 s** / 中位 **3.3 s** / p90 **14.1 s** / 最大 **51.5 s**。

### 1.3 工具调用构成（去重后 ≈ 410 次）

| 工具 | 次数 | 占比 |
| --- | --- | --- |
| `read_file` | 178 | 43% |
| `run_tests` | 86 | 21% |
| `ls` | 76 | 19% |
| `glob` | 26 | 6% |
| `write_file` | 17 | 4% |
| `edit_file` | 14 | 3% |
| `run_build` | 5 | 1% |
| 其它 | ~8 | 2% |

**探索类（read_file + ls + glob）= 280 次 = 68%；真正产出代码的（write + edit）= 31 次 = 7.6%。**

### 1.4 run_tests 的无效率

| 项 | 次数 |
| --- | --- |
| `run_tests` 调用总数 | 86 |
| 真正执行了测试 | **24（28%）** |
| 被拒绝、什么都没跑 | **62（72%）** |
| ├ 预算耗尽（`budget exhausted for Integration`） | 23 |
| └ 层不匹配（`The active TDD layer is 'Integration'`） | 39 |

### 1.5 工作区门禁（快盘，正常）

```
15:00:32  模板复制（28 文件）
15:00:43  后端依赖就绪   11 s / 252 包
15:01:12  前端依赖就绪   29 s / 219 包
15:01:17  Smoke check passed（前端构建）
15:01:20  286 task(s) scheduled
```

合计 **45 秒**，与上一轮在 C 盘验证的结论一致 —— **输出目录放快盘这条规避措施有效**。

---

## 2. 正向验证：生成的应用确实能跑

- `/api/health` → `{"code":200,"message":"Backend Ready"}`
- `GET /` → 正常返回 Vite 构建产物 HTML（`/assets/index-*.js` + `index-*.css`）
- 生成的组件：`HomeAccountLinks.tsx`、`HomeHeroCarousel.tsx`、`HomeQuickGuide.tsx`、`HomeSearchPanel.tsx`
- 生成的测试：2 个 Playwright E2E + 5 个前端 Integration
- **REQ-1.1 的 E2E 实测通过**：`ok 1 test-e2e\home-default-page.spec.js › Display the default home page (607ms)` / `1 passed (3.5s)`

即：arc-agent 的"需求 → 接口 → 测试 → 实现"闭环本身是成立的，问题在吞吐。

---

## 3. 顺带发现的两个环境级缺陷

### 3.1 Playwright 浏览器未预装（首次节点白烧约 4 分钟）

`@playwright/test@1.57.0` 需要 `chromium_headless_shell-1200`，但机器上只有
`chromium_headless_shell-1228`（属于 arc-bench runner 的 playwright 版本）。
arc-agent 全流程**没有任何 `playwright install` 步骤**（`app_type_handler/` 与 `core/` 里 grep 不到），
于是 E2E 连续 3 次失败：

```
Error: browserType.launch: Executable doesn't exist at
  ...\ms-playwright\chromium_headless_shell-1200\chrome-headless-shell-win64\chrome-headless-shell.exe
║ Please run the following command to download new browsers:  npx playwright install ║
```

**智能体自己想办法修好了**——它编辑生成的 `backend/package.json`，把
`playwright install chromium chromium-headless-shell` 塞进 `db:prepare:e2e` 脚本。
从 15:18 到 15:22 花了约 4 分钟、3 次失败运行、2 次文件编辑，才把环境补齐。

**代价不止一次**：因为这个修复住在 `db:prepare:e2e` 里，而 `_prepare_e2e_database()` 在**每次** E2E 调用前都会跑
`npm run db:prepare:e2e` —— 于是**每次 E2E 运行都会 spawn 一次 `playwright install`**（已缓存，但仍有进程 + 网络校验开销）。
这是单次 E2E 耗时 25–50 s 的重要组成。

### 3.2 沙箱文件权限把模型引向死路

日志里出现多次无效探索，全部因为权限拒绝：

```
Error: permission denied for read on /workspace/frontend/dist
Error: permission denied for read on /workspace/backend/node_modules
Error: permission denied for read on /                          （glob 默认根）
Error: permission denied for write on /tmp/_system_response.json
Error: Path '/frontend/tests': path_not_found                  （缺少 /workspace 前缀）
```

这些不是模型笨，是**权限边界和路径约定没有前置告知**。

---

## 4. 架构级提速点（按收益排序）

### P0-1　串行调度是结构性天花板（`core/workflow.py:342`）

**现象**：286 个任务严格串行，一次只有一个任务在飞。

**根因**：

```python
# core/workflow.py:329-342
@staticmethod
def _max_concurrent_tasks() -> int:
    ...
    # Hard cap at 1 until _execute_task runs each node in an isolated workspace.
    return min(max(1, value), 1)
```

代码注释已经写明了原因：并发任务**共享同一个 workspace、同一个 Git 仓库、同一个 phase runner、
同一份 checkpoint 状态**；而 `GitClient.commit()` 执行 `git add .`，并发下会把别的节点的未提交改动
一起提交进去。所以这不是"忘了开并发"，是**架构上还没有做隔离**。

**改法**：引入 **per-node workspace**（`git worktree add` 或目录级复制 + 软链接共享 `node_modules`），
把 `git add .` 换成 `git add <node 自己的路径>`，然后解除 `min(..., 1)` 上限。

**预期收益**：12306 有 117 个叶子、大量互不依赖的兄弟节点。模型等待占 53% 且是网络等待（可重叠），
并发 4–8 时墙钟时间理论上降到 1/3–1/5。**这是唯一能把 27 小时压到 5 小时以内的手段。**

> 注意：`node_modules` 若每节点复制会爆盘，必须用软链接或共享目录 + 只读挂载。

### P0-2　三个阶段各自重新探索工作区（68% 的工具调用浪费）

**现象**：同一个文件被反复读。以 `HomePage.tsx` 为例，一个节点内被读了 **7 次**：

| 阶段 | 读 `HomePage.tsx` 次数 |
| --- | --- |
| InterfaceDesigner | 2 |
| TestGenerator | 2 |
| TestDrivenDeveloper | 3 |

`vite.config.js` / `test/setup.ts` / `main.tsx` / `index.css` / `App.tsx` / `package.json` / `index.html`
各被读 3 次（InterfaceDesigner 2 + TestGenerator 1）。

**根因（两层）**：

1. **三个阶段是三个独立会话。** thread_id 互不相通：
   ```python
   # agents/interface_designer.py:106
   f"{ns}:{node_id}:DESIGN:InterfaceDesigner"
   # agents/test_generator.py:121
   f"{ns}:{node_id}:DESIGN:TestGenerator"
   # agents/test_driven_developer.py:223
   f"{ns}:{node_id}:IMPLEMENT:TestDrivenDeveloper:{layer}"
   ```
   `InMemorySaver` 按 thread_id 复用会话，所以 DESIGN 读过的文件对 IMPLEMENT 完全不可见。

2. **首轮上下文里没有真实文件清单。** `_get_project_structure()` 拿到的是
   `app_type_handler/web.py:886` 的 `project_structure_lines()` —— 那是一段**静态规则文本**
   （"Backend source root: backend/"），不是工作区实际文件列表。`agents/context/pipeline.py` 里
   唯一的 `os.walk` 在 `app_type_handler/base.py:151`（模板复制），**没有任何地方把真实文件树注入提示词**。
   于是每个阶段、每个节点都要从零 `ls`/`glob`/`read_file` 重新发现一遍同一个固定模板。

   `_get_source_file_cards()` 虽然存在，但它只服务"本节点已声明的接口文件"，首次 DESIGN 时是空的。

**改法（三选一或叠加）**：

- **A（最便宜）**：编译启动时对模板/工作区做一次快照，生成 `<workspace_manifest>`（路径 + 行数 + 模板文件全文），
  作为**静态层**注入所有阶段的系统提示。模板只有 28 个文件，一次性注入成本极低。
- **B**：在 `NodeContextCache` 之上加一层 **run 级文件内容缓存**，key = `(path, mtime_ns, size)`，
  跨阶段、跨节点共享；`read_file` 命中时直接返回缓存内容。
- **C**：让三个阶段共享 thread_id 前缀（同一节点一个会话），DESIGN 的读取对 IMPLEMENT 可见。

**预期收益**：探索类调用从 280 次降到 50 次以内 → 直接砍掉约 230 次工具往返，
按每次往返约 6–8 s 计，**单节点省 25 分钟量级**（跨节点累计）。

### P0-3　TDD 层切换设计缺陷导致 72% 的 run_tests 是空转

**现象**：86 次 `run_tests`，只有 24 次真的跑了测试。62 次被拒，两类原因：

| 拒绝原因 | 次数 | 触发条件 |
| --- | --- | --- |
| `budget exhausted for Integration` | 23 | 该层预算已 10/10 |
| `The active TDD layer is 'Integration'` | 39 | agent 想跑 E2E，但当前活跃层还是 Integration |

**根因（`core/phases.py:317-452`）**：`run_requested_tests` 里

```python
if selected_type != active_test_type:
    return ("Exit Code: 1\nSTDERR:\n"
            f"The active TDD layer is `{active_test_type}`, but run_tests requested `{selected_type}`. ...")
```

**层切换只能由外层 while 循环在 agent 会话返回之后完成**，而 agent 收到
`"- The system will advance to the next test layer: E2E."` 之后，**自然反应就是再调一次 `run_tests` 去推进** ——
结果被拒，再试，再被拒。39 次空转由此产生。

更糟的是**绿灯重跑**：日志里的测试批次 4–9 是**同一批 4 个 Integration 文件、结果都是 `4 passed / 21 passed`、
exit 0 的重复运行，连跑了 6 次**。

**改法**：

1. 允许 `run_tests(type=下一层)` **原地推进层**：把 `active_test_type` 切过去并直接执行，而不是报错。
   这一条同时消灭两类拒绝。
2. 加**结果短路**：若同一测试文件集合 + 文件指纹未变化且上次已通过，直接返回缓存结果，不再 spawn runner。
   （构建侧已有现成的指纹复用机制，`"Reused the existing frontend/dist because ... (fingerprint 550e411aee2a)"`，
   把同一思路搬到测试结果上即可。）

**预期收益**：run_tests 调用 86 → 约 25，省掉 60+ 次工具往返与配套模型轮次；
同时省掉 6 次重复的 vitest 执行（每次约 10–15 s）。

### P1-1　预算耗尽后没有中断钩子（空转）

**现象**：Integration 预算 10/10 耗尽后，agent 仍在同一个会话里反复调 `run_tests` 拿
"budget exhausted"，从 15:13:35 一直转到 15:16 前后才推进到 E2E。日志里 `budget exhausted` 累计出现 **74 次**。

**根因**：

- `DEFAULT_RECURSION_LIMIT = 5000`（`agents/runtime/runners.py:17`）—— 形同不设上限。
- 现有"停止"机制只是 `StageDisciplineMiddleware` **屏蔽工具调用**，不终止会话；
  LangGraph 循环本身没有中断钩子（上一轮诊断已记录为"仍待处理"）。
- `max_sessions = 10 × 层数` 只约束**会话之间**，约束不了会话内部的空转。

**改法**：预算耗尽时抛出 LangGraph `interrupt` / 用 `Command(goto=END)` 结束会话；
或退一步，在 `build_agent_config()` 里给每个会话设一个小的轮次上限（如 40）。
同时把 `DEFAULT_RECURSION_LIMIT` 从 5000 降到合理值（如 200），避免任何异常路径无限跑。

### P1-2　环境门禁只覆盖前端构建，不覆盖 E2E 运行器

`verify_workspace()` 只跑 `npm run build`。Playwright 浏览器、E2E runner、`db:prepare:e2e` 都没进门槛。

结果就是 §3.1：第一个叶子节点替全流程发现了"浏览器没装"，代价约 4 分钟 + 3 次失败运行。

**改法**：门禁扩展为
（1）`playwright install chromium chromium-headless-shell`（幂等、有缓存）；
（2）跑一条最小 E2E 冒烟用例确认浏览器真的能起来；
（3）确认 `db:prepare:e2e` 可执行。
并且把浏览器预装**从 `db:prepare:e2e` 里挪到工作区初始化**，避免每次 E2E 调用都 spawn 一次 install。

### P1-3　上下文随会话单调增长（checkpointer 复用的副作用）

`InMemorySaver` 让 TDD 重试复用同一会话，这是好事（避免冷启动重读）。
但会话只增不减：每轮重试都把上一轮的完整测试输出（`BEGIN RAW TEST OUTPUT` 整块，动辄上千行）追加进历史，
提示词越来越长 → 单轮延迟越来越高（实测 p90 14.1 s / max 51.5 s，且 TestDrivenDeveloper 的 101 轮里大量是长上下文轮次）。

**改法**：给复用的会话加**上下文压缩**——测试原始输出只保留摘要 + 尾部 N 行进历史，
完整输出落到文件并在提示里给路径。

### P2　杂项

1. **`append_debug_log` 每次调用 `open("a")` + close**（`core/logging.py:50`）。
   29 分钟产生 6191 行 / 1.1 MB，全量跑约 60 MB。建议改成常驻句柄 + 批量 flush。
2. **spinner 每 0.25 s 写一次 stdout**（`core/cli.py:66`）。重定向到文件时全部落盘，
   29 分钟 compile.log 已 **747 KB**，全量跑约 40 MB。建议 spinner 只写 TTY，不进日志文件。
3. **`execute` 工具被禁用**（`agents/runtime/factory.py:30` 的 `DISABLED_BUILTIN_TOOLS`）。
   这是安全考量，但代价是**智能体没有任何装依赖的手段**——上一轮已经因此踩过
   "缺 `@testing-library/dom` → 手写 shim → 必然失败 → 烧光 10 次预算"。
   本次又踩了"缺 Playwright 浏览器 → 改 package.json"。建议给一个**受限的依赖安装工具**
   （只允许 `npm install <白名单包>` / `playwright install`，且走模板预声明优先）。
4. **路径约定未前置**：`/frontend/tests` 报 `path_not_found`、`glob` 默认落到 `/` 报 permission denied。
   建议在系统提示里明确"所有路径必须以 `/workspace/` 开头"，并把 glob 的默认根钉死在 workspace。
5. **视觉分析单次 37 s**（`homepage.png`）。已有 `.arc/visual_analysis_cache.json` 按
   `(path, mtime, size, prompt_version)` 缓存，跨节点复用，这点没问题；但 26 张参考图首次全跑约 16 分钟，
   建议在门禁阶段**预热**（并行发起），别让它落在节点关键路径上。

---

## 5. 收益估算

以本次实测 28.7 min / 5 任务为基线，逐项叠加：

| 措施 | 影响面 | 估算 |
| --- | --- | --- |
| P0-2 注入文件清单 + 跨阶段读缓存 | 工具调用 280 → <60 | 单节点省约 60% 的探索往返 |
| P0-3 层原地推进 + 绿灯短路 | run_tests 86 → ~25 | 省 60+ 次往返 + 6 次重复执行 |
| P1-1 预算耗尽即中断 | 消除 74 次空转 | 单节点省 1–3 分钟 |
| P1-2 门禁补 E2E | 一次性 | 省首个节点约 4 分钟 + 每次 E2E 的 install 开销 |
| P1-3 上下文压缩 | 降低单轮延迟 | p90 14 s → 目标 <10 s |
| **P0-1 per-node workspace + 并发** | 墙钟直接除 | **÷3 ~ ÷5** |

**保守估计：只做 P0-2 + P0-3 + P1-1，单节点耗时降一半以上；再做 P0-1（并发 4），
全量从 27 小时压到 2–4 小时量级。**

---

## 6. 复现方式

```bash
# 1) 环境（现成 venv，模型 MiniMax-M3）
C:/Users/25137/.workbuddy-ai/binaries/python/envs/arc/Scripts/python.exe arc_main.py doctor

# 2) 编译（输出必须放快盘；关掉沙箱 safe-delete）
export CODEBUDDY_SAFE_DELETE_ENABLED=0
cd D:/code/arc-agent
python arc_main.py compile \
  "D:/code/arc-bench/arc-bench/webapp/12306/requirements" \
  -o "C:/Users/25137/AppData/Local/Temp/arc-run-12306" \
  -t web --port 3301 --clean

# 3) 解析耗时
python C:/Users/25137/AppData/Local/Temp/arc_stats.py <debug.log>   # 工具调用/轮次构成
python C:/Users/25137/AppData/Local/Temp/arc_split.py <debug.log>   # 模型等待 vs 工具执行
```

产物与日志：

```
C:\Users\25137\AppData\Local\Temp\
├── arc-run-12306\          # 本次工作区（含 .arc/debug.log、processing_queue.json）
├── arc-run-12306.log       # CLI 日志（747 KB）
├── arc_stats.py            # 工具调用/轮次统计脚本
└── arc_split.py            # 墙钟时间拆分脚本
```

> 说明：本次运行在 29 分钟后主动终止，未跑完全量。所有"全量耗时"数字均为按已完成任务的线性外推。
