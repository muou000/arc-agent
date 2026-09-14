# 用 ARC-Bench「12306」对 arc-agent 做全量全新压测 —— 实测报告

日期：2026-09-13 23:43 – 2026-09-14 02:13
被测：`D:\code\arc-agent`（main @ `c61b250` + 1 处本地修复，见 §5.9）
基准：`D:\code\arc-bench\arc-bench\webapp\12306`（143 节点 / 117 叶 / 286 任务 / 135 条基准用例）
输出：`C:\Users\25137\AppData\Local\Temp\arc-full-12306`
产物：`D:\code\arc-bench\arc-bench\webapp\12306\metrics-full-20260913\`（`metrics.json` / `quota_split.json` / `valid_window.json` / `node_outcomes.json` / `appendix.md`）

---

## 0. 一句话结论

**286 个任务确实全部"跑完"了，但其中 260 个是模型额度耗尽后被 429 瞬间判死的，不是真实结果。**
本次运行只有 **前 47 分钟（23:50:37–00:37:51）是有效测量**：这 47 分钟里只结算了 **26 个任务（9%）**、
13 个节点走到终态，花了 **¥39.85 / 6735 万 tokens**，然后 MiniMax Token Plan 的用量上限被打穿
（`已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量`），此后 112 次模型调用全部失败。

按有效窗口实测外推，跑完整套 12306 需要 **约 7.4 亿 tokens / ¥438**，是本次账号可用额度的 **11 倍**。
**所以真正的问题不是"agent 能不能做"，而是"当前的成本结构根本跑不完这个基准"。**

三个可量化的成本结构问题（都在有效窗口内实测，与额度无关）：

| 问题 | 实测 | 直接后果 |
| --- | --- | --- |
| 上下文缓存读取占成本的 **67%** | 每次模型调用携带 ~41k cached tokens，缓存命中率 96.25%，cache_read 花掉 ¥26.86 / ¥39.85 | 降 context 就是降成本，且是唯一的大头 |
| 探索类工具调用占 **89%** | 2514 次调用里 2244 次是 read_file/ls/grep/glob，产出文件的只有 215 次（8.6%） | 每任务 86 次探索 vs 8 次写文件 |
| run_tests **74% 空转** | 39 次调用只有 10 次真跑了测试（2 过 8 挂），29 次被"预算耗尽/层不匹配"拒绝 | 白白多出 29 次工具往返 + 配套模型轮次 |

另外发现 **并行 worktree 的合并冲突是有效窗口内的头号失败原因**：12 个节点因冲突失败，
全部撞在共享文件上（`frontend/src/App.tsx` 6 次、`HomePage.tsx` 3 次、`init_db.js` 3 次、`auth.js` 3 次…），
而同一时段 API 错误为 **0**。即这些失败与额度无关，是架构性的。

---

## 1. 这次测试是怎么跑的

| 项 | 值 |
| --- | --- |
| 命令 | `arc_main.py compile <12306/requirements> -o <C:\...\arc-full-12306> -t web --port 3301 --clean` |
| 模式 | 全新（`--clean`），`ARC_NODE_WORKTREES=1`（每任务独立 git worktree + 端口槽 + E2E 库） |
| 并发 | `ARC_MAX_CONCURRENT_TASKS=4`（16 逻辑核；上一轮并发 3 已在共享文件上冲突，故只小幅上调） |
| 模型 | MiniMax-M3，`chat_completions`，`https://api.minimax.cn/v1` |
| 其它 | `ARC_AUTO_TDD_RETRY=1`（默认）、视觉预计算默认开启、`CODEBUDDY_SAFE_DELETE_ENABLED=0` |
| 环境 | Windows / Node v22.22.2 / npm 10.9.7 / Python 3.13.14 |

输出落在 C 盘而非基准目录，是**有意的**：D 盘对 npm 类负载慢 7–13 倍（证据见 §5.8），
指标、日志、报告仍留在基准目录里。

---

## 2. 关键结论：这次运行的有效性边界

`compile` 在 01:00:07 以 `rc=1` 结束，队列 286/286 全部结算，表面看"跑完了"。但把 debug 日志按时间切开后：

| 阶段 | 时间 | 时长 | 模型调用 | 429 错误 | 结算任务 |
| --- | --- | --- | --- | --- | --- |
| 脚手架 + 门禁 | 23:43:48–23:45:20 | 1.6 min | 0 | 0 | 0 |
| 参考图预计算 | 23:45:21–23:50:37 | 5.3 min | 0 | 0 | 0 |
| **有效窗口（节点循环）** | **23:50:37–00:37:51** | **47.2 min** | **1,563（全部成功）** | **0** | **26** |
| 额度雪崩 | 00:37:51–00:58:19 | 20.5 min | 0 | 112 | 254 |
| 收尾 TDD 重试 | 00:58:19–01:00:07 | 1.8 min | 0 | 6 个节点 | 6 |
| 端到端评测 | 01:00:13–02:13:02 | 72.8 min | 0 | 0 | — |

两条硬证据说明 00:37:51 之后的部分不是 agent 行为：

1. `runner-events.jsonl` 里 **1,563 条 `llm_usage` 全部早于 00:37:51，之后 0 条**——额度墙后没有任何一次模型调用成功。
2. 有效窗口内 **API 错误 = 0、合并冲突 = 12**；雪崩期 **API 错误 = 112、合并冲突 = 5**。

被 429 判死的 260 个任务里，有 44 个显示为 `COMPLETED`——那是非叶节点在"没有本地测试"时的直接收敛，
不是真实产出。所以下面所有分析都以有效窗口为准。

---

## 3. 有效窗口的实测数据

### 3.1 产出（47 分钟）

| 口径 | 数量 |
| --- | --- |
| 结算任务 | 26 / 286（**9%**） |
| 任务完成 / 失败 | 8 / 18 |
| 其中 DESIGN 完成 / 失败 | 4 / 9 |
| 其中 IMPLEMENT 完成 / 失败 | 4 / 9 |
| 走到终态的节点 | 13 / 143 |
| 叶子节点真正 PASSED | **0** |
| 追溯产出 | 接口 50（已实现 7）、测试 62（Unit 19 / Integration 29 / E2E 14）、调用边 7、节点契约 0 |

关于"2 个 PASSED 叶子（REQ-2.2.1 / REQ-2.2.7）"：两者 DESIGN→IMPLEMENT 各只用 5 秒，
`tests.json` 里 **没有为它们登记任何测试**，所以是"无测试直通"，不能算功能通过。
有效窗口内唯一真正跑通的测试批次属于 REQ-2.1.7（Unit 1/10 通过、Integration 2/10 通过），而它随后死在合并冲突上。

### 3.2 耗时

| 项 | 值 |
| --- | --- |
| 有效窗口墙钟 | 2,833 s（47.2 min） |
| 吞吐 | **0.551 任务/分钟**（并发 4） |
| 模型等待（4 路求和） | 6,840 s → 占可用流时间 **52.9%** |
| 工具执行（4 路求和） | 1,105 s → **8.5%** |
| 其余（图步进/上下文拼装/日志/git） | **38.6%** |
| 模型轮次 | 1,575（与 1,563 次模型调用互为交叉验证） |
| 单轮延迟 均值/中位/p90/最大 | 4.34 / 1.75 / 9.02 / **160.8** s |

分角色：

| 角色 | 轮次 | 等待合计 | 均值 | 中位 | p90 | 最大 |
| --- | --- | --- | --- | --- | --- | --- |
| TestGenerator | 702 | 3,174 s | 4.52 | 1.57 | 7.06 | 160.8 |
| InterfaceDesigner | 514 | 2,496 s | 4.86 | 1.91 | 12.40 | 130.5 |
| TestDrivenDeveloper | 359 | 1,170 s | 3.26 | 2.06 | 7.81 | 37.8 |

**注意：并发 4 相比上一轮并发 3 没有提速。** 上一轮（09-13 19:45，并发 3，D 盘）是 0.568 任务/分钟，
本轮是 0.551 任务/分钟——吞吐持平。加并发没换来吞吐，但换来了更多合并冲突（见 §5.3）。

### 3.3 Token 与成本

| 项 | 值 |
| --- | --- |
| 模型调用 | 1,563 次（全部在有效窗口内） |
| input | 2,489,487 |
| output | 924,397（其中 reasoning 278,349） |
| **cache read** | **63,940,922** |
| cache write | 0 |
| **total** | **67,354,806** |
| 缓存命中率 | **96.25%** |
| **成本** | **¥39.85** |

成本构成与单位经济：

| 成本项 | CNY | 占比 |
| --- | --- | --- |
| cache_read | 26.855 | **67.4%** |
| output | 7.765 | 19.5% |
| input | 5.228 | 13.1% |

| 单位指标 | 值 |
| --- | --- |
| 每次模型调用 tokens | **43,088**（其中 cache read 40,909） |
| 每次模型调用成本 | ¥0.0255 |
| 每个结算任务 | 2.59M tokens / **¥1.53** / 60 次模型调用 / 97 次工具调用 |
| 外推全量 286 任务 | **7.41 亿 tokens / ¥438** |

阶段分布很关键：**DESIGN 花掉 ¥31.01（78%）、IMPLEMENT ¥8.84（22%）**，
DESIGN 用了 1,230 次调用（79%）。单节点最贵的两个是 REQ-2.2.3（¥5.64 / 189 次）和
REQ-2.1.7（¥5.53 / 192 次），两个节点占了整张账单的 28%。

---

## 4. 端到端结果

编译结束后启动生成的应用，用基准自带的 135 条 Playwright 用例打它：

| 项 | 结果 |
| --- | --- |
| `/api/health` | `{"code":200,"message":"Backend Ready"}` |
| `GET /` | 200（前端构建产物正常托管） |
| **通过 / 失败** | **0 / 135** |
| 套件用时 | 72.8 min（135 条每条 60 s 超时，2 并发） |
| 对照：参考实现（`12306/project`） | **133 / 135** |

**0/135 这个数字不能读作"arc-agent 能力为 0"**：编译只推进了 9% 的任务，
REQ-3/4/5/6 全部未实现（登录、订单、支付、行程指南整块缺失），
失败的是超时而不是断言——应用"能起、能响应"，但功能不存在。
这次运行能支撑的结论只是：**链路是通的（模板/依赖/构建/单端口托管/健康检查都正常），
但没有任何一个完整功能被验证过。**

---

## 5. 分析

### 5.1 成本的第一性结构：每次调用背着 41k 缓存上下文

1,563 次调用 / 67.35M tokens → 每次 43k tokens，其中 40.9k 是 cache read。
按 `agents/model/costing.py` 的 MiniMax-M3 单价（cache_read ¥0.42/M、input ¥2.1/M、output ¥8.4/M），
cache read 虽然单价只有 input 的 1/5，但体量是它的 26 倍，所以独占 67% 的成本。

这解释了为什么"缓存命中率 96%"看起来漂亮却依然很贵：**缓存只是把重读上下文的单价打了折，
没有减少上下文本身。** 成本 ≈ 上下文规模 × 轮次，而这两个数都很大（41k × 1,575）。

顺带一个可核对的事实：`cache_write` 恒为 0，说明 provider 侧没有单独计费缓存写入
（与 costing.py 的注释一致），缓存是白拿的——**这也意味着减少重复上下文比"提高命中率"更值钱。**

### 5.2 探索占 89%：三个阶段各自从零发现同一个工作区

| 工具 | 次数 | 占比 |
| --- | --- | --- |
| read_file | 1,158 | 46.1% |
| ls | 474 | 18.9% |
| grep | 411 | 16.3% |
| **探索小计** | **2,244** | **89.3%** |
| write_file | 136 | 5.4% |
| glob | 128 | 5.1% |
| edit_file | 79 | 3.1% |
| **产出小计** | **215** | **8.6%** |
| run_tests | 39 | 1.6% |

按角色看，重复最重的是 TestGenerator（1,130 次调用里 536 次 read_file + 241 次 ls + 206 次 grep）：

| 角色 | 调用 | read_file | ls | grep | write/edit |
| --- | --- | --- | --- | --- | --- |
| TestGenerator | 1,130 | 536 | 241 | 206 | 74 |
| InterfaceDesigner | 888 | 380 | 150 | 125 | 110 |
| TestDrivenDeveloper | 496 | 242 | 83 | 80 | 31 |

这与 09-12 报告的判断完全一致，且这次更严重（89% vs 68%）。
根因没变：三个阶段是三个独立 thread_id 会话，DESIGN 读过的文件对 IMPLEMENT 不可见；
首轮上下文里没有真实文件清单，只有静态规则文本。**每次读取都是一次模型轮次，
每次轮次都重新背着 41k 上下文——探索和成本是直接相乘的。**

### 5.3 并行 worktree 的合并冲突：有效窗口内的头号失败原因

有效窗口 12 个冲突、0 个 API 错误。逐节点归因（全部 143 节点）：

| 根因 | 节点数 | 占比 |
| --- | --- | --- |
| quota_429（额度雪崩） | 108 | 75.5% |
| ok（含 21 个非叶收敛 + 2 个无测试叶子） | 23 | 16.1% |
| **merge_conflict** | **12** | **8.4%** |

冲突文件（这是问题的核心——**全是共享文件**）：

| 文件 | 冲突次数 |
| --- | --- |
| `frontend/src/App.tsx` | 6 |
| `frontend/src/pages/HomePage.tsx` | 3 |
| `backend/src/database/init_db.js` | 3 |
| `backend/src/routes/auth.js` | 3 |
| `frontend/src/components/registration/RegistrationForm.tsx` | 3 |
| `frontend/src/components/login/LoginForm.tsx` | 3 |
| `frontend/src/api/auth.ts` | 2 |
| `backend/src/services/user_service.js` | 2 |
| 其它（LoginPage/RegistrationPage/RegisterPage/app.js/seed_db） | 各 1 |

机制很清楚：REQ-1.1 / REQ-1.2 / REQ-2.1.1 是三个兄弟节点，都要往 `HomePage.tsx` 里加东西；
并发 4 让它们在各自 worktree 里同时改同一个文件，合并回 master 时必然冲突。
`core/worktree.py` 的设计注释写的是"兄弟节点在常见情况下触碰不相交的文件"——
**在这个需求树上，这个前提不成立**：`App.tsx`（路由注册）、`app.js`（路由挂载）、
`init_db.js`（建表）天然是所有兄弟节点的公共写点。

冲突的代价不止失败本身，还有连锁反应。日志里能看到一条完整的级联：

```
[REQ-2.1.2] `run_tests` Unit failed for an environmental reason
            (unresolved import: ../../../src/components/registration/countries);
            the workspace is broken, not the implementation. Stopping the TDD loop
```

即：DESIGN 阶段合并冲突 → 工作区缺文件 → 测试 import 失败 → 被判定为"环境级失败"短路。
好消息是 §5.7 的环境短路确实生效了（没有烧满 10 次预算），坏消息是**它短路的是一个本可避免的失败**。

### 5.4 run_tests 74% 空转（老问题，量级变小但比例更高）

| 结果 | 次数 |
| --- | --- |
| 真正执行并失败 | 8 |
| 真正执行并通过 | 2 |
| **被拒：预算耗尽** | **18** |
| **被拒：层不匹配** | **11** |

39 次调用只有 10 次（26%）真的跑了测试。与 09-12 报告（72% 空转）相比比例没改善。
两类拒绝的根因还是 `core/phases.py` 的层切换只能由外层 while 循环在 agent 会话返回之后完成，
而 agent 收到"系统将推进到下一层"后自然反应就是再调一次 `run_tests`。

### 5.5 会话内的空转与递归上限

- `GraphRecursionError: Recursion limit of 300 reached` 出现 **2 次**（REQ-2.2.3 @ 00:31:51、REQ-2.1.7 @ 00:32:03），
  两个节点直接崩掉。上限已经从 5000 收到 300，但 300 轮对一个叶子节点仍然偏高，
  且撞上限时是"抛异常终止"而不是"带着部分成果优雅收敛"。
- 预算耗尽后 agent 仍在会话内反复拿 `budget exhausted`——09-12 报告记录的"缺中断钩子"问题依然存在。

### 5.6 收尾 TDD 重试撞在额度墙上

`ARC_AUTO_TDD_RETRY=1` 在 00:58:19 对 6 个失败节点发起第二轮 IMPLEMENT，
而这 6 次全部落在额度墙之后（00:58–01:00），**100% 无效**。
这次只是 6 个节点、2 分钟，量级小；但它暴露了一个成本控制问题：
**重试阶段没有任何额度感知**，如果额度在运行中途耗尽，重试会继续按计划烧完。

### 5.7 正面验证：环境级失败短路确实生效

`[environment failure] ... the workspace is broken, not the implementation. Stopping the TDD loop`
在有效窗口内出现，且相关节点没有出现 10+10+10 次预算烧光。
09-12 报告里 P1"验证层短路"的修复在真实运行中被触发并正确工作——这是本次运行里少数几个可确认为"已修复"的项。

### 5.8 环境侧：D 盘对 npm 类负载慢 7–13 倍（已绕开）

第一次启动时输出放在基准目录（D 盘），门禁卡住：后端 `npm install` 154 s、前端 145 s，
而单线程、无并发干扰的 peer 补丁跑了 9 分 30 秒仍未结束。改到 C 盘后：

| 操作 | C:\Temp | D:\code | 倍数 |
| --- | --- | --- | --- |
| 后端 npm install | 12–14 s | 154 s | 11–13× |
| 前端 npm install | 18–20 s | 145 s | 7–8× |
| peer 补丁（单线程） | 43–44 s | >570 s（未跑完） | >13× |
| 前端 vite build | 6–10 s | 未测到 | — |
| **纯文件创建**（300 文件/15 目录） | 0.144 s | 0.152 s | **1.0×** |

最后一行是关键：**纯文件创建两盘无差异**，所以这不是磁盘吞吐问题，
而是 npm 的访问模式（大量 stat/rename/symlink）撞上了 D 盘的安全软件钩子。
09-12 报告测到的"25–37 倍元数据税"如今只对 npm 生效，且已降到 7–13 倍。
**结论不变：编译输出必须放快盘。**

### 5.9 运行前修复的一个 P0 回归（否则会白烧 117 个节点）

`5ef61f5`（把模板同步为官方 provision）删掉了模板里声明的 `@testing-library/dom` peer，
改为由运行时补装——但 `app_type_handler/web.py::_ensure_testing_library_dom` 的补装命令
**漏了 `--legacy-peer-deps`**，在 npm 10.9.7 上必然触发 arborist 崩溃
（`Cannot read properties of null (reading 'edgesOut')`）。而这条补丁恰恰只在
主安装已经回退到 `--legacy-peer-deps` 的世界里才会执行，所以它是**必然失败**的。

后果链条与 09-12 记录的 P0-4 完全一致：所有生成的前端组件测试 import 即挂，
agent 没有 `execute` 工具无法自救，每个受影响节点烧光 run_tests 预算。

修复：给补丁命令补上该 flag；新增 `tests/test_app_type_handler/test_testing_library_dom_peer.py`（3 条，全过）。
实测前端 vitest 从 `Cannot find module '@testing-library/dom'` 变为 1 passed。
**这次修复是有价值的**：本轮有效窗口内确实跑出了 2 个通过的测试批次（REQ-2.1.7 的 Unit 与 Integration），
没有这个修复它们不可能通过。

---

## 6. 优化方向

按"对本次实测数据的杠杆"排序。每条都给出本次测到的基线，便于回归对比。

### P0 — 决定"能不能跑完"的成本结构

**P0-1　压缩每次调用携带的上下文（最大杠杆，预期省 40–60% 成本）**
基线：43,088 tokens/次，其中 40,909 是 cache read；cache read 占成本 67%。
做法（按性价比）：
1. **一次性注入工作区清单**：编译启动时快照模板/工作区（路径 + 行数 + 关键文件全文），
   作为静态层注入所有阶段系统提示。模板只有 27 个文件，成本极低，
   但能直接消灭 §5.2 的重复探索（同时省 token 和轮次）。
2. **run 级文件内容缓存**：key = `(path, mtime_ns, size)`，跨阶段跨节点共享，`read_file` 命中直接返回。
3. **测试原始输出只留摘要 + 尾部 N 行**入历史，完整输出落文件并给路径——
   现在整块 `BEGIN RAW TEST OUTPUT` 会追加进会话历史，让后续每一轮都背着它。

**P0-2　让 run_tests 不再空转（预期砍掉 29 次无效往返 / 26 个任务）**
基线：39 次调用 29 次被拒（18 预算 + 11 层不匹配）。
做法：`run_tests(type=下一层)` 允许原地推进 `active_test_type` 并直接执行，而不是报错返回；
再对"同一测试文件集合 + 指纹未变 + 上次通过"加结果短路（构建侧已有现成的指纹复用机制可抄）。

**P0-3　额度感知与预算闸门（防止这次这种"跑完但无效"）**
基线：¥39.85 耗尽账号额度，之后 260 个任务、112 次调用、22 分钟全部白费。
做法：
1. 编译启动时读取/配置额度预算（tokens 或 CNY），达到阈值即**优雅停止并落盘 `--resume` 状态**，
   而不是让每个节点各撞一次 429。
2. 识别 429 的 `usage cap`（额度用尽）与 `rate limit`（瞬时限速）：前者应立即终止整轮编译，
   后者才值得退避重试。现在两者走同一条路径。
3. 收尾 TDD 重试（`ARC_AUTO_TDD_RETRY`）开始前先做一次额度探针。

### P1 — 决定"能不能跑对"

**P1-1　worktree 合并语义：共享文件是结构性瓶颈（有效窗口头号失败原因）**
基线：12/12 有效窗口失败节点都是合并冲突，全撞在 `App.tsx`/`app.js`/`init_db.js`/`HomePage.tsx` 等公共文件。
可选做法（可叠加）：
1. **公共写点串行化**：把 `frontend/src/App.tsx`、`backend/src/app.js`、`backend/src/database/init_db.js`
   这类"注册表型"文件声明为互斥资源，改动它们的任务排队执行（其余仍并行）。
2. **改注册方式**：让 agent 写 `routes/*.js` 并在 `app.js` 里只做一次通配挂载，
   或引入"按文件合并"策略（对已知注册表文件用 AST/追加式合并而非 git 三方合并）。
3. **冲突后自动重试**：冲突时用最新的 master 重建 worktree 并重放本节点的文件改动（而不是直接判死）。
   现在冲突即 `FAILED`，worktree 留在盘上等人看——这在无人值守的全量跑里等于永久失败。

**P1-2　探索去重（预期省 30–40% 轮次）**
基线：89% 的调用是探索类，其中 read_file 1,158 次。
做法：三阶段共享 thread_id 前缀（同节点一个会话）；`ls`/`glob`/`read_file` 按 `(path, mtime)` 做短期缓存，
重复命中直接返回"未变化"；`glob` 默认根钉死在 workspace（日志里仍有 `permission denied for read on /`）。

**P1-3　递归上限与中断钩子**
基线：2 次 `Recursion limit of 300 reached` 直接崩节点；预算耗尽后仍在会话内空转。
做法：给每个会话设小轮次上限（如 40）并**带部分成果优雅收敛**；
预算耗尽时用 LangGraph `interrupt` / `Command(goto=END)` 结束会话，而不是靠 middleware 屏蔽工具调用。

**P1-4　DESIGN 阶段单独优化（它占 78% 成本）**
基线：DESIGN ¥31.01 / 1,230 次调用 vs IMPLEMENT ¥8.84 / 333 次。
DESIGN 不需要最强的模型：模型分级（DESIGN 与测试生成用快模型，只在 IMPLEMENT 用强模型）
在 09-12 报告里提过，本轮数据进一步支持它——DESIGN 是绝对大头。

### P2 — 收尾

1. **日志**：1.16 小时产生 debug.log 5.4 MB + 控制台日志 3.2 MB，其中大量是 spinner 噪声；
   `append_debug_log` 每次调用都 `open("a")` + close。建议 spinner 只写 TTY，日志改常驻句柄 + 批量 flush。
2. **视觉预计算**：5.3 分钟全落在关键路径前。建议并行度提高或与脚手架阶段重叠。
3. **`records/` 与新增测试未纳入 git**：本次新增的
   `tests/test_app_type_handler/test_testing_library_dom_peer.py` 尚未跟踪。

### 关于"重跑"的前置条件

账号的 Token Plan 用量上限**已用尽且未恢复**（复测返回同一 429：
`已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量`）。
按有效窗口外推，跑完整套 12306 需要约 7.41 亿 tokens / ¥438。
**建议先做 P0-1 + P0-2 再重跑**：这两项直接压缩 tokens/任务与无效轮次，
否则即使升额也只是把 ¥438 的账单原样付掉一次，而且大概率仍然被合并冲突（P1-1）拉低通过率。

---

## 7. 复现方式

```bash
# 环境
C:/Users/25137/.workbuddy-ai/binaries/python/envs/arc/Scripts/python.exe arc_main.py doctor

# 全量编译（输出必须在快盘；关掉沙箱 safe-delete）
export CODEBUDDY_SAFE_DELETE_ENABLED=0
export ARC_NODE_WORKTREES=1
export ARC_MAX_CONCURRENT_TASKS=4
cd D:/code/arc-agent
python arc_main.py compile \
  "D:/code/arc-bench/arc-bench/webapp/12306/requirements" \
  -o "C:/Users/25137/AppData/Local/Temp/arc-full-12306" \
  -t web --port 3301 --clean

# 指标采集（本报告全部数字的来源）
python C:/Users/25137/AppData/Local/Temp/arc-prep/collect_metrics.py \
  --workspace "C:/Users/25137/AppData/Local/Temp/arc-full-12306" \
  --console-log "<compile log>" --out "<out dir>"
python C:/Users/25137/AppData/Local/Temp/arc-prep/analyze_full.py  --workspace <ws>   # 429 切分
python C:/Users/25137/AppData/Local/Temp/arc-prep/valid_window.py                    # 有效窗口统计
python C:/Users/25137/AppData/Local/Temp/arc-prep/node_outcomes.py                   # 逐节点归因
python C:/Users/25137/AppData/Local/Temp/arc-prep/render_tables.py ... --out appendix.md

# 端到端评测（打生成应用）
bash C:/Users/25137/AppData/Local/Temp/arc-prep/bench_eval.sh "<ws>" 3399
# 或手动：cd <ws>/backend && PORT=3399 node src/index.js
#        cd D:/code/arc-bench && TARGET_URL=http://127.0.0.1:3399 node scripts/run-playwright.js --app 12306
```

一键驱动：`bash C:/Users/25137/AppData/Local/Temp/arc-prep/run_full.sh`

---

## 8. 原始数据

见同目录 `appendix.md`（A–M 节：运行标识与时间 / 产出与收敛 / 吞吐与外推 / 时间构成 / 工具调用 /
run_tests 有效性 / 失败分类 / Token 与成本 / 端到端评测 / 运行前预检证据 / 额度切分 /
逐节点失败归因 / 有效窗口行为统计），以及 `metrics.json`、`quota_split.json`、
`valid_window.json`、`node_outcomes.json` 四份结构化原始数据。

---

## 9. 与历史运行的口径对照

| 项 | 09-12（串行） | 09-13 19:45（并发 3，D 盘，未跑完） | **本次（并发 4，C 盘，全量）** |
| --- | --- | --- | --- |
| 模式 | 共享工作区串行 | worktree ×3 | worktree ×4 |
| 有效时长 | 24 min | 30 min | **47 min** |
| 结算任务 | 2.5 / 143 节点 | 17 / 286 | **26 / 286** |
| 吞吐 | — | 0.568 任务/分 | **0.551 任务/分** |
| tokens | 未统计 | 29.7M | **67.35M** |
| 成本 | 未统计 | ¥17.32 | **¥39.85** |
| 每任务 tokens | — | 1.75M | **2.59M** |
| 探索类占比 | 70% | 68% | **89%** |
| run_tests 空转率 | 72% | 72% | **74%** |
| 头号失败原因 | 环境坏掉（模板空） | 合并冲突 | **合并冲突（12）+ 额度耗尽（108）** |

两点值得注意：
- **并发 3 → 4 没有换来吞吐**（0.568 → 0.551 任务/分），但冲突面扩大。继续加并发之前应先修 P1-1。
- **每任务 tokens 从 1.75M 涨到 2.59M（+48%）**。节点分布不同（本轮有更多 REQ-2.2.x 重节点）
  只能解释一部分，另一部分疑似上下文随会话增长——这正好是 P0-1 第 3 条的靶子。
