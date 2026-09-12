# 用 ARC-Bench「12306」压测 arc-agent — 诊断报告

日期：2026-09-12
被测：`D:\code\arc-agent`（main @ `2d933ca`）
基准：`D:\code\arc-bench\arc-bench\webapp\12306`（117 个 spec 文件 / 135 条 `test()`）

---

## 0. 一句话结论

**这次全量运行不成立，而且失败的原因不是 arc-agent 的智能体能力，是工程配置。**
编译是在「工作区没有任何模板」的状态下跑的，生成出来的应用根本装不上依赖、构建不起来，
于是每个节点的 TDD 验证必然全败并烧光 10 次重试预算。按这个状态跑满 143 个节点，
只会得到一份约 20–30 小时、通过率接近 0 的产物。

已定位 **3 个阻断级缺陷**（含 1 个根因）和 **4 个性能热点**，并**实测验证了修复方案有效**。

---

## 1. 先确认：基准测试链路本身是好的

用 arc-bench 自带的参考实现（`12306/project`）做对照，全量结果：

| 项目 | 结果 |
| --- | --- |
| 通过 | **133** |
| 失败 | **2** |
| 用时 | 2.2 min |

参考实现自己挂的 2 条（属基准自身问题，不该算 arc-agent 的账）：

- `REQ-4.3.7 Save a valid security mailbox update` — 改完邮箱登出后，
  `clickNamed(page,'LOGIN')` 等 `getByRole('button',{name:/login/i})` 超时；
  页面上登录入口实际是 `<a>Login</a>` 链接而非 button。
- `REQ-5.2.7 Submit the booking form with an unavailable ticket class` —
  `helpers.selectPassengerForBooking` 里 `checkbox.check()` 之后 `toBeChecked()` 始终 unchecked。

**基线 = 133/135。** 另外注意口径：README 写的是 117 条，实际 `test()` 是 **135** 条。

---

## 2. 阻断级缺陷

### 2.1 【根因】模板目录是空的，编译没有模板可用

`app_type_handler/base.py:18-24` 期望模板位于：

```
<repo>/arc-template/templates/web-react-express/
```

但该目录 **只有空的 `backend/src`、`backend/test-e2e`、`frontend/src`、`frontend/test`，0 个文件**。
`arc-template/` 既不是 git 子模块（无 `.gitmodules`），也没被 git 跟踪，也不在 `.gitignore` 里——
就是一个空壳。而真正完整可用的模板在 **`<repo>/template/`**（28 个文件，
`template.yaml` 里写着 `id: web-react-express`）。

`copy_template()` 只用 `os.path.exists(template_dir)` 判断，空目录照样返回成功，
所以日志照常打印 "Template ready"，**没有任何告警**。

后果链条：

```
空模板 → 工作区只有空目录
      → 智能体以为要从零建工程，自己手写 package.json / vite.config / vitest.config
      → 手写的依赖组合（react 18 + vite 5 + vitest 5）无法解析
      → npm install 失败、node_modules 为空
      → npm run build 失败（vite is not recognized）
      → 每个节点的 Integration / E2E 验证 100% 失败
      → TDD 循环把 10 次预算全部烧光
```

**修复（已验证）**：把 `template/` 的内容放到 `arc-template/templates/web-react-express/`，
或用环境变量 `ARC_AGENT_TEMPLATES_ROOT` 指向一个包含 `web-react-express/` 的目录。

验证结果（用临时模板根跑真实编译）：

| 阶段 | 修复前 | 修复后 |
| --- | --- | --- |
| 后端 `npm install` | 3 s，0 个包（空转） | **90 s，252 个包** |
| 前端 `npm install` | 4 s，0 个包（空转） | 见 2.2（需配合修复） |
| 前端 `npm run build` | `vite is not recognized` | **1.95 s 成功构建** |

### 2.2 前端依赖装不上：npm arborist 在 peer 解析时崩溃

即使模板正确，前端 `npm install` 仍然失败，报：

```
npm error Cannot read properties of null (reading 'edgesOut')
  at #loadPeerSet (@npmcli/arborist/lib/arborist/build-ideal-tree.js:1289)
```

排查过程：与 lockfile 无关（移走过期的 `package-lock.json` 后同样崩）。
真实原因是 npm 10.9.7 解析 `vitest` 的 optional peer 时崩溃——
解析链是 `vitest@4.x → @vitest/browser-playwright@5.0.0 → peer vitest@5.0.0`。

**修复（已验证）**：加 `--legacy-peer-deps`。

```
npm install --legacy-peer-deps   →  215 个包 / 60 个 .bin，31 s
npm run build                    →  1.95 s 成功
```

### 2.3 依赖安装失败被静默吞掉

`app_type_handler/web.py:24-38` 的 `run_npm_install`：

```python
_, stderr = await process.communicate()
if process.returncode == 0:
    await _emit_log(log_cb, "System", f"NPM install success in {target_dir}")
```

三个问题：

1. 只看退出码，**不校验结果**（`node_modules` 是否真的非空）；
2. 失败分支的日志文案 `NPM install failed in ...` **没有在 `core/cli.py` 里做映射**，
   所以 CLI 界面上完全不显示——用户只看到 "Installing frontend packages" 之后就没了；
3. `stderr` 被捕获但从不使用，排查时拿不到 npm 的真实报错。

顺带：`create_subprocess_shell("npm install")` 在 Windows 下走 `cmd.exe`，
没有传 `env=`，也没做 npm 可执行文件的显式解析。

### 2.4 模板缺 `@testing-library/dom`，与 P0-2 的 `--legacy-peer-deps` 回退互相引爆

端到端验证时，首个叶子节点 REQ-1.1 的 Integration **连续 10 次全败**，而且原因不是生成代码：

```
@testing-library/react@16 把 @testing-library/dom 声明为 peerDependency
        ↓
前端安装必须回退到 --legacy-peer-deps（npm 10 arborist 崩溃，见 2.2）
        ↓
--legacy-peer-deps 不安装 peer 依赖
        ↓
node_modules/@testing-library/dom 缺失
        ↓
每个组件测试 import 即失败，TDD 的 10 次预算全烧在同一个「不可能修好」的原因上
```

**更严重的是**：智能体**没有任何安装依赖的工具**（模型原话："I can't run npm commands"）。
它只能自己想办法——手写 shim 并在 `vite.config` 里加 alias：

```js
const domShim = path.resolve(__dirname, 'tests/_shims/testing-library-dom.mjs')
resolve: { alias: { '@testing-library/dom': domShim } }
```

alias 解析出的路径在 vitest 里变成 `/c:/Users/...`，加载失败。

即：**一个缺失的依赖 → 智能体无法修复 → 该节点必然失败 → 白烧 10 次预算 × 117 个叶子。**

**修复（已验证）**：在模板的 `frontend/package.json` 里直接声明 `@testing-library/dom`。
实测补上后 `npm install --legacy-peer-deps` 装入 288 个包，
`node_modules/@testing-library/dom` 是**真实安装**（`dist/` + `types/` + `README.md` + `LICENSE`），
而不是之前那个只有 3 个文件的手写桩。

### 2.5 模板契约测试在安装失败时静默跳过

`tests/test_template_contract/test_template_frontend.py`：

```python
if proc.returncode != 0:
    pytest.skip(f"npm install failed: {proc.stderr or proc.stdout}")
```

这个测试**本该是模板的守门人**，但它用的是裸 `npm install`（不带 `--legacy-peer-deps`，
与生产路径不一致），失败时又 `pytest.skip` —— 于是 arborist 崩溃和
`@testing-library/dom` 缺失被它一起掩盖了。
性质与 2.1 的 `copy_template` 完全相同：**守门人沉默，缺陷直达生产。**

**修复**：改用与生产一致的 `--legacy-peer-deps`，失败改为 `pytest.fail`
（无 node 环境已由模块级 `skipif` 覆盖），并新增一条断言——
`@testing-library/dom` 必须是含 `dist/` 的真实安装、而非桩。
修复后该文件 6 条全过（此前 5 过 1 跳过）。

### 2.6 模板目录的最终形态：单一布局 + 纳入版本控制

P0-1 的修复（把模板放到 `arc-template/templates/web-react-express/`）最初住在一个
**未被 git 跟踪**的目录里（`?? arc-template/`，28 文件 / 0 跟踪）——
别人 clone 仓库后模板照样缺失，原 bug 直接复发。
而 `<repo>/template/` 是被跟踪的（28 文件），两份内容逐字节一致，形成重复。

**最终方案：只保留正式布局。** 仓库里只留 `arc-template/templates/<id>/`：

1. `arc-template/templates/web-react-express/` 已 **纳入版本控制**（28 文件）。
   这是多 app_type 的正式布局——`android` / `cli` 将来各自放这里。
2. 遗留的 `<repo>/template/` 已用 `git rm -r` 移除。Git 识别为 **rename**
   （`R template/... -> arc-template/templates/...`），历史得以保留；
   移除前 `diff -rq` 逐字节比对通过，内容零丢失。
3. `template_candidates()` 简化为单一来源：`ARC_AGENT_TEMPLATES_ROOT` 覆盖，
   否则 `<repo>/arc-template/templates/<id>`。**回退逻辑已删除。**

> 过程中曾短暂引入过一个回退（指向 `<repo>/template`），并因此产生一个回归：
> 回退对任何 app_type 都生效，导致 Android / CLI 编译会**静默拿 web 模板去搭工程**
> 而不是报"模板缺失"。单一布局方案把这个风险连同回退一起删掉了，
> 并由 `test_each_app_type_gets_its_own_directory` 钉住
> "每个 app_type 只找自己的目录"。

**同步更新**：`.gitignore` 的模板路径改为 `arc-template/templates/*/...`（通配 app_type）；
`tests/test_template_contract/` 三个文件的 `TEMPLATE_ROOT` / `TEMPLATE_BACKEND` /
`TEMPLATE_FRONTEND` 全部指向正式布局。

**端到端验证**（真实调用）：`copy_template()` → `True`，28 文件落盘，
`template.yaml` / `backend/package.json` / `backend/src/app.js` / `frontend/package.json` /
`frontend/vite.config.js` / `frontend/src/App.tsx` 齐全，`@testing-library/dom` 已在依赖里。

---

## 3. 主要耗时在哪里

以 24 分钟的中断运行（143 节点里只推进了 2.5 个）为样本，从 `debug.log` 统计：

### 3.1 模型推理占了约 75% 的墙钟时间（最大头）

| 指标 | 数值 |
| --- | --- |
| 模型轮次 | 141 |
| 模型等待合计 | 1119 s ≈ **18.7 min / 24 min** |
| 单轮中位 / 均值 | 4 s / 7.9 s |
| p90 / 最大 | 20 s / **56 s** |

按角色拆分：TestDrivenDeveloper 82 轮 / 611 s，InterfaceDesigner 47 轮 / 417 s，
TestGenerator 12 轮 / 91 s。

结论：**这是硬成本**，且当前用 MiniMax-M3 单轮动辄 20–56 秒。
117 个叶子 × 每节点数十轮 → 单是模型等待就是十几小时。

### 3.2 70% 的工具调用是重复探索，不是干活

| 工具 | 次数 |
| --- | --- |
| `ls` | 91 |
| `read_file` | 77 |
| `glob` | 63 |
| `write_file` | 52 |
| `run_tests` | 23 |
| `run_build` | 6 |
| `edit_file` | 2 |

`ls + glob + read_file = 231 次，占 328 次总调用的 70%`，而真正产出代码的
`write_file + edit_file` 只有 54 次。

典型浪费（REQ-1.1 的 DESIGN 阶段，5.8 分钟）：

- 反复 `ls` 同一批目录（`/workspace`、`/workspace/frontend`、`/workspace/backend` 来回十几遍）；
- `search_interfaces` 连查 6 次，每次都是 `{"count": 0}`；
- `glob` 不带路径时默认落到 `/`，返回 `Error: permission denied for read on /`，重试 3 次以上；
- 因为工作区是空的，智能体花了 2.5 分钟才"确认"项目是空的。

### 3.3 TDD 验证循环在环境坏掉时无限空转

REQ-1.1 一个节点的 IMPLEMENT 阶段（>13 分钟仍未结束）：

- Integration：`usage 1/10` → `10/10`，**10 次全部 Exit Code 1**（11:54:50 → 11:58:01）；
- 预算耗尽后，**又继续空转 6 次** "budget exhausted"（11:58:22 → 12:01:44，白烧 3.5 分钟）；
- 然后进入 E2E 层，同样立刻失败（12:03:05）。

即：一个必然失败的节点，会稳定消耗 10+10+10 次测试执行。

### 3.4 队列严格串行

`ARC_MAX_CONCURRENT_TASKS` 默认 1（`8d672db` 刻意为之，因为三个阶段 agent、
git checkpoint、测试运行器都作用于同一个 workspace）。12306 有 117 个叶子，
串行是 20+ 小时的结构性原因。

---

## 4. 优化改进方案（按优先级）

### P0 — 必须先修，否则任何全量跑都无意义 ✅ 已实现并验证

1. ✅ **补齐模板目录**：`arc-template/templates/web-react-express/` ← `template/` 的内容（28 文件）。
   同时 `copy_template()` 加了模板可用性校验（必须含 `template.yaml` 且除 manifest 外至少还有一个文件），
   空模板直接报错退出，不再静默继续。
2. ✅ **npm 安装健壮化**：两次尝试（`npm install` → `npm install --legacy-peer-deps`）；
   安装后校验 `node_modules` 非空（`node_modules_ready()`）；失败时把 stderr 尾部打进日志；
   `core/cli.py` 补了 `NPM install failed` / `NPM install error` 的映射，失败在 CLI 上可见。
3. ✅ **环境前置门禁**：新增 `verify_workspace()`——依赖装完后先跑一次 `npm run build`，
   失败即中止编译，不进入 143 个节点的循环。CLI 上以 `Verify Smoke check` 行呈现。

单测覆盖：`tests/test_app_type_handler/test_workspace_gates.py`（24 条，全过）。

### P0（第二轮，端到端验证中暴露）✅ 已实现并验证

4. ✅ **模板补声明 `@testing-library/dom`**（见 2.4）：`frontend/package.json` 直接声明该 peer，
   否则 `--legacy-peer-deps` 不装它，所有组件测试必然 import 失败且智能体无法自救。
5. ✅ **模板契约测试不再静默跳过**（见 2.5）：改用与生产一致的 `--legacy-peer-deps`，
   失败改 `pytest.fail`，并断言该依赖是真实安装。
6. ✅ **模板收敛为单一正式布局**（见 2.6）：`arc-template/templates/<id>/` 纳入版本控制，
   遗留的 `<repo>/template/` 已 `git rm` 移除（Git 识别为 rename，历史保留），
   回退逻辑一并删除。仓库里现在只有一份模板，不存在漂移。

> **遗留风险**：`records/`、`tests/test_app_type_handler/test_workspace_gates.py`、
> `tests/test_app_type_handler/test_failure_classification.py` 目前仍**未被 git 跟踪**。
> 模板本身已解决（28 文件已 staged）。

### P1 — 直接压缩耗时

4. ✅ **验证层短路**（已实现并测试）：新增 `app_type_handler/test_results.py::classify_test_failure()`，
   把「环境级失败」与「断言级失败」分开。命中环境级（缺依赖 / 未解析 import / 测试器没装 /
   `node_modules` 为空 / 缺 npm script）时：

   - 该层立即停止，**不再重试**（原先是 10 次全烧）；
   - **后续层直接跳过**（工作区已坏，Unit 挂了 Integration/E2E 必然同样挂）；
   - 失败摘要标记为 `[environment failure] ...`，并在 CLI 上以
     `environment failure (...); TDD loop stopped` 单独呈现。

   实测（`tests/test_agents/test_tdd_loop_e2e.py`）：一个环境坏掉的节点，
   **测试执行次数从 10（Unit）+10（Integration）+10（E2E）= 30 次降到 1 次**。

   > **仍待处理**：agent *会话*本身不会提前结束——LangGraph 循环没有中断钩子，
   > 现有停止机制只是通过 middleware 屏蔽工具调用。所以模型仍会空转若干轮
   > 反复拿到 "budget exhausted"。这些轮次单轮只要几秒，远小于它们不再触发的
   > 测试执行（每次约一分钟），但仍是可省的成本。
5. **探索去重**：`ls`/`glob`/`read_file` 占 70% 的调用量，且大量重复。
   - 工具默认路径收敛到 workspace（消除 `glob` 打到 `/` 的 permission denied）；
   - 把"工作区结构 + 已有文件清单"一次性注入首轮上下文，别让模型自己探索；
   - 对同一路径的重复 `ls`/`glob` 结果做短期缓存或直接返回"未变化"。
6. **模型分级**：DESIGN 和测试生成用快模型，只在 TDD 实现阶段用强模型；
   单轮 20–56 秒的延迟是当前最大的单点成本。
7. **并行调度**：真正提速必须做 per-node workspace，然后才敢把
   `ARC_MAX_CONCURRENT_TASKS` 调到 >1。117 个叶子里有大量无依赖的兄弟节点可以并行。

### P2 — 收尾打磨

8. **日志**：spinner 每秒写盘，24 分钟产生 **650 KB / 7214 行**日志，其中 **63% 是纯 spinner 噪声**
   （9-11 那次 40 分钟跑到 884 KB）；关键路径同步 `open('a')` 写盘。
   建议 spinner 不进日志文件，日志改批量/异步刷盘。
9. **`write_file` 长内容参数异常**：模型输出的工具参数偶尔被结构化包裹成
   带 `$text`/`main`/`div` 键的对象，导致写大文件反复失败
   （REQ-1 因此空转约 5 分钟）。虽然最终文件没被污染（已扫描确认），
   但 harness 侧缺一层参数归一化/修复，值得补。

### 环境侧（与 arc-agent 无关，但会影响结论）

10. **沙箱 safe-delete 会污染编译**：WorkBuddy 沙箱通过 `PYTHONPATH` 注入
    `sitecustomize.py` 删除守卫，批量删除（>50 文件）会抛
    `SAFE_DELETE_BULK_CONFIRM_REQUIRED`。后果：`compile --clean` 直接失败，
    且在沙箱内跑长编译等于给基准引入偏差。
    **规避**：启动编译时 `export CODEBUDDY_SAFE_DELETE_ENABLED=0`。
11. **输出目录必须放快盘**（详见 §5）：D 盘 `\code` 下新建文件的元数据开销是 C 盘 `%TEMP%` 的 25–37 倍。
    全量编译请用 `-o C:\...\Temp\<out>`，不要用 `-o D:\code\...\<out>`。

---

## 5. 宿主机 I/O 性能异常（环境侧，直接决定验证落点）

排查中发现一个与 arc-agent 代码无关、但会显著扭曲所有编译耗时数据的宿主机问题。

### 5.1 现象：D 盘 `\code` 下创建文件比 C 盘慢 25–37 倍

用同一脚本（写 1000 个 400 字节小文件，分散在 50 个子目录）做**交错对照**，
排除时间顺序带来的干扰：

| 场景 | 耗时 | 相对 C 盘 |
| --- | --- | --- |
| D 盘，新建目录 + 新文件 #1 | 121.09 s | 26× |
| D 盘，新建目录 + 新文件 #2 | 141.06 s | 37× |
| D 盘，覆盖已有文件 | 24.78 s | 5× |
| C 盘（`%TEMP%`），新建 #1 | 4.73 s | 1× |
| C 盘（`%TEMP%`），新建 #2 | 3.78 s | 1× |
| C 盘（`%TEMP%`），覆盖已有 | 4.56 s | 1× |

关键点：**代价集中在「创建新目录条目」这个元数据操作上**——
D 盘覆盖已有文件只要 24.8 s，新建却要 121–141 s。
这是典型的杀软 / EDR 实时扫描钩住文件创建（scan-on-write）的特征，
不是磁盘吞吐问题，也与 WorkBuddy 沙箱无关（关掉沙箱后实测 394 s，反而更慢）。

### 5.2 对编译的实际影响

同一次编译（12306，`--clean`），只改 `--output-dir` 的落点：

| 阶段 | 输出在 D 盘 | 输出在 C 盘 |
| --- | --- | --- |
| 后端 `npm install` | 164 s（252 包） | **11 s**（252 包 / `.bin` 69） |
| 前端 `npm install` | >7 min 未完成，`.bin` 为空 | **30 s**（214 包 / `.bin` 60） |
| 前端 `npm run build` | — | **4 s 成功** |
| 工作区门禁合计 | >10 min（未走完） | **45 s** |

每个节点的 IMPLEMENT 都要重跑构建与测试（每次都是成千上万次小文件写入），
这个税会在 286 个任务上反复叠加。

### 5.3 结论与对策

**全量编译必须把 `--output-dir` 放在快盘（C 盘 `%TEMP%` 或 SSD 根目录），不要放在 `D:\code` 下。**
这是当前环境下提升吞吐最廉价、最直接的一招——不需要改任何代码。
它同时解释了"9-11 那次 40 分钟只推进 2.5 个节点"的一部分原因（那次输出也在 D 盘）。

---

## 6. 实测数据附录

### 6.1 时间线（修复前的坏运行，11:44–12:08）

| 时间 | 事件 |
| --- | --- |
| 11:44:07 | 编译启动 |
| 11:44:08–11:44:15 | 脚手架 + 依赖安装（3 s / 4 s，**空转**） |
| 11:44:22–11:44:24 | ROOT DESIGN（非叶，跳过） |
| 11:44:24–11:48:57 | REQ-1 DESIGN（4.5 min，含 46 s 视觉分析） |
| 11:48:57–11:54:43 | REQ-1.1 DESIGN（5.8 min） |
| 11:54:43–12:08+ | REQ-1.1 IMPLEMENT（>13 min，仍在失败重试中被中断） |
| — | 143 节点里完成 2.5 个 |

### 6.2 P0 修复的端到端验证（真实编译，输出在 C 盘）

`arc_main.py compile <12306/requirements> -o %TEMP%\arc-verify-12306 -t web --port 3301 --clean`

```
13:52:15  RequirementLoader  Loading requirement model
13:52:15  System  Workspace  Scaffolding project template
13:52:15  System  Workspace  Template ready                     ← P0-1 模板补齐生效
13:52:15  System  Workspace  Web stack configured
13:52:15  System  Deps       Installing backend packages
13:52:26  System  Deps       backend packages ready             ← 11 s / 252 包
13:52:26  System  Deps       Installing frontend packages
13:52:56  System  Deps       frontend packages ready            ← 30 s / 214 包
13:52:56  System  Verify     Smoke check: building frontend
13:53:00  System  Verify     Smoke check passed                 ← P0-3 门禁生效
13:53:0x  InterfaceDesigner  REQ-1  ...                          ← 正常进入节点循环
```

| 检查项 | 结果 |
| --- | --- |
| 模板复制 | 28 个文件，`template.yaml` + `backend/` + `frontend/` 齐全 |
| 后端依赖 | 252 个包，`.bin` 69 个 |
| 前端依赖 | 214 个包，`.bin` 60 个（修复前在 D 盘 `.bin` 恒为 0） |
| 前端构建 | `dist/index.html` + `assets/`，冒烟检查 4 s 通过 |
| 智能体视角 | REQ-1 直接读到模板的 `App.tsx` / `HomePage.tsx` / `api/index.ts` / `app.js` / `database/*`，**没有再出现"自建 test.txt 草稿"的探索空转** |

结论：**P0-1（模板）、P0-2（npm 健壮化 + `--legacy-peer-deps` 回退）、P0-3（前置门禁）
三项修复在真实编译中全部生效，编译可以正常穿过工作区阶段进入节点循环。**

### 6.3 产物留档

```
D:\code\arc-bench\arc-bench\webapp\12306\
├── _prev-run-20260911\          # 9-11 中断运行 + 9-12 被污染的两次尝试
├── _validate\                   # 模板修复验证用的工作区
├── output\                      # 输出在 D 盘的那次（受 I/O 异常拖慢）
└── compile.log

C:\Users\25137\AppData\Local\Temp\
├── arc-verify-12306\            # P0 修复后的端到端验证产物（推荐落点）
└── arc-verify.log               # 对应编译日志
```
