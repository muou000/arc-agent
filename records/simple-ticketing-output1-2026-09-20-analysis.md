# simple-ticketing arc-output1 运行分析：耗时优化点与错误清单

日期：2026-09-20
运行产物：`D:\code\simple-ticketing\arc-output1\.arc\`（debug.log 1.5MB / 8,594 行，runner-events.jsonl 717 事件）
需求：火车票预订演示系统（ROOT + REQ-1 注册 + REQ-2 登录，REQ-2 声明依赖 REQ-1）
**运行时代码版本：`c59f7da`（"Merge pull request #78 chore/agent-skills-config"，2026-09-20 16:22）——不是当前 main。**

版本判定依据：debug.log 第 5 行模板路径 `D:\code\arc-agent\arc-template\...` 表明编译器从 `D:\code\arc-agent` 主 worktree 运行；该 worktree 的 `git reflog show main` 显示 HEAD 在 16:23:26 落在 c59f7da 后直到 21:22:33 才再次移动（run 窗口 19:32→20:27 完全落在其中）；`git merge-base --is-ancestor e4082d0 c59f7da` 为否——**PR #77 的修复不在本次运行代码里**。这一条决定了下文最大错误的定性。

---

## 0. 结论摘要

1. **本次运行判负的直接原因（REQ-2/ROOT 永久 BLOCKED）已被 PR #77 修复，但修复晚于本次运行合入主仓**——本地主 worktree 当时停在 PR #77 合入之前的 c59f7da。这是"过期检出"问题，不是 main 上的活缺陷。用当前 main `--resume --retry-failed` 即可从本产物继续跑完。
2. **55.4 分钟里 3 并发槽位只用了约 1 个**（1 task in flight 占 46 分钟）。原因不是调度器坏，而是队列形状：REQ-2:DESIGN 被"依赖方 DESIGN 等依赖 IMPLEMENT 完成"的门禁卡住 36.9 分钟。该门禁的流水线化正是已开票的 Issue #83（杠杆②）。
3. **REQ-1:IMPLEMENT 第一次尝试 29.4 分钟全烧在一个真产品缺陷上**：worktree 里 `backend/src/app.js` 的 SPA fallback `res.sendFile(path.join(...))` 找不到 `frontend/dist`，Integration 两个用例红 → TDD 死磕 10/10 预算耗尽判负。agent 自己定位+修复又花了约 22 分钟（含 7 次后端运行时 teardown/重启、5 次完整 E2E 重跑），auto-TDD-retry 第二次尝试 7.5 分钟收尾全绿。
4. 剩余时间大头是正常的：LLM 推理墙钟 29.2 分钟（53%），其中 DESIGN 阶段结构合理（15.2 分钟产出 22 接口 + 8 测试清单）。
5. 次级浪费：run_tests 调用间 759s 的 LLM 思考间隙；E2E 每次全量重启后端（每次 ~150s 的调用里大头是后端启动+Playwright 冷启动）；4 次 auth-session-consistency skill 重复读取。

**如果三个问题都不发生，本运行理想墙钟 ≈ 25–28 分钟（REQ-2 与 REQ-1:IMPLEMENT 并行 + 不烧 29 分钟死磕），即当前 55.4 分钟的约一半。**

---

## 1. 运行时间线（全部来自 debug.log 行号）

| 时间(本地) | 行号 | 事件 | 时长 |
| --- | --- | --- | --- |
| 19:32:37 | 1 | 编译开始 | — |
| 19:32:37–19:33:02 | 3–17 | 模板拷贝、npm 安装（backend 9s/Playwright 4s/frontend 22s）、前端构建验证 | 25s |
| 19:33:03 | 19 | Git 初始化、加载 6 任务队列 | 1s |
| 19:33:03–19:33:54 | 21–28 | 视觉参考预计算（3 图并发，4 并发上限） | 51s |
| 19:33:54–19:35:54 | 30–380 | **ROOT:DESIGN**（含合并） | 2.0 min |
| 19:35:54–19:51:03 | 381–1637 | **REQ-1:DESIGN**（InterfaceDesigner 19:35:55→19:41:42，TestGenerator 19:41:43→19:51:01，含合并） | 15.2 min |
| 19:51:03–20:20:30 | 1638–6913 | **REQ-1:IMPLEMENT 尝试 1 → 失败** | 29.4 min |
| 20:20:30 | 6910–6913 | 跳过集成、IMPLEMENT FAILED、ROOT/REQ-2 标记 BLOCKED | <1s |
| 20:20:30–20:27:59 | 6914–8592 | **REQ-1:IMPLEMENT 尝试 2（auto-TDD-retry）→ PASSED、合并** | 7.5 min |
| 20:27:59 | 8593 | `Compilation finished without an accepted result (blocked: REQ-2, ROOT)` | — |

槽位利用率（按分钟粒度统计在飞任务数，容量 3）：

| 在飞任务数 | 持续 | 说明 |
| --- | --- | --- |
| 0 | 2 min | setup/视觉预计算 |
| 1 | 46 min | 全程几乎单线程 |
| 2 | 8 min | 仅 IMPLEMENT 尝试 1 的 tdd-retry 重叠段计数 |
| 3 | 0 min | **从未打满** |

槽位空闲 106/168 槽·分钟 = **63%**。

## 2. Token 与成本（runner-events.jsonl，254 个 llm_usage 事件）

| 节点:阶段 | 调用 | LLM 墙钟 | input | output | cache_read | reasoning | 成本 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ROOT:DESIGN | 15 | 1.8 min | 41,301 | 22,856 | 305,993 | 16,121 | $0.41 |
| REQ-1:DESIGN | 31 | 10.4 min | 326,929 | 104,226 | 1,385,233 | 64,064 | $2.14 |
| REQ-1:IMPLEMENT 尝试 1 | 148 | 12.1 min | 365,163 | 86,672 | — | — | $7.03 |
| REQ-1:IMPLEMENT 尝试 2 | 60 | 5.0 min | 232,843 | 26,098 | — | — | $2.51 |
| **合计** | **254** | **29.2 min** | 966,236 | 239,852 | 19,147,510 | 147,573 | **$12.09** |

- LLM 墙钟占总窗口 53%（29.2/55.4 min）；缓存命中率极高（cache_read 是裸 input 的 ~20 倍，缓存工作正常）；reasoning 占输出 62%；**零次 API 重试**（attempts>1 为 0）——网络层健康，慢不在模型基础设施。
- 尝试 1 花 $7.03 产出的是"失败"，占了全运行成本 58%。其中 top 单次调用 $0.333（in=96,526，REQ-1:DESIGN 收尾的合同汇总）。
- 最长单次 185.9s（REQ-1:DESIGN，11:39:01）。

---

## 3. 错误清单（按严重度）

### E1【P0·已修复，运行未含】REQ-2/ROOT 永久 BLOCKED_BY_DEPENDENCY，运行判负

**现象**：20:20:30.348/.349（行 6912–6913）ROOT、REQ-2 因"prerequisite REQ-1 failed"被标记 BLOCKED；随后 20:20:30.566（行 6914）auto-TDD-retry 重置 REQ-1 并重跑成功（20:27:59 PASSED 合并），但 REQ-2/ROOT 的 BLOCKED 从未被解除，运行以 `blocked: REQ-2, ROOT` 收场。

**根因**：运行时代码（c59f7da）的 `_propagate_dependency_blocks` 是单向的——失败传播阻塞，但**没有任何解除路径**。`_prepare_auto_tdd_retry`（c59f7da 版 core/workflow.py:1095）重置 REQ-1 后直接 `_drain_runnable_tasks`，不会回头释放已 BLOCKED 的依赖方。

**修复状态**：PR #77（`e4082d0` "fix(workflow): unblock dependents after a successful retry and survive unspawnable npm"，2026-09-20 15:58，+ 评审提交 `e08bec6`）新增 `_release_dependency_blocks`（现 main core/workflow.py:730）并在两个入口调用（编译起点 retry plan，行 386；`_prepare_auto_tdd_retry`，行 1220）。已合入 origin/main（16:35）——**但本地 `D:\code\arc-agent` 主 worktree 直到 21:22:33 才 pull**，本次 19:32 启动的运行跑的是旧代码。

**行动项**：跑评测前确认主 worktree HEAD（`git -C D:\code\arc-agent log -1`）；本产物可用当前 main 续跑（见 §6）。

### E2【P0·真产品缺陷+测试难点】SPA fallback 在 worktree 中找不到 frontend/dist，Integration 2 用例红 → 尝试 1 判负

**现象**：Integration 批次 `backend/tests/app.test.js` 的两个用例持续红（debug.log 行 3379、3510、3620、3774、3949、4143、4293、4438、4570、4717 共 10 轮）：

- `keeps the SPA fallback serving HTML on non-/api GET routes`：`AssertionError: expected [ 200, 503 ] to include 404`（行 3330）
- `does not route unknown paths under /api/auth to the SPA fallback`（行 3339）

agent 的修复路径（全程可见）：20:10:29（行 5754）改 `frontendDistPath` 解析 → 20:13:03（行 5920）引入 `resolveFrontendDistPath()` 多候选路径探测 → 20:15:36（行 6112）发现日志没有 `[app.js] frontend dist resolved at:` → 20:18:02（行 6148/6224）最终修复成功：`[app.js] frontend dist resolved at: D:\code\simple-ticketing\arc-output1\.arc\worktrees\REQ-1\frontend\dist`。

**根因**：worktree 的 `frontend/dist` 种子拷贝（core/worktree.py:583 `_seed_frontend_dist`）本身正常，但模板 `backend/src/app.js` 用 `path.resolve(__dirname, '../../frontend/dist')` 这种固定相对路径假设主工作区布局，在 `.arc/worktrees/REQ-1/` 下解析到错误位置。属于**官方模板与 worktree 布局的契约缺口**——按仓库规则，模板修复必须走 `app_type_handler/template_patches.py` 定向补丁（AGENTS.md「App-type handler 和模板」）。

**修复状态**：未修。建议开票：template patch 让 SPA fallback 的 dist 解析兼容 worktree（多候选 + 启动日志已有，但需在模板层面预置，而不是靠 agent 临场修 22 分钟）。

### E3【P1】尝试 1 的 TDD 预算耗尽形态：Unit 10/10、Integration 10/10 耗尽，E2E 8/10 通过，但批次状态判负

行 6866–6868：`TDD batch Unit did not pass after 10/10 run_tests call(s); budget exhausted` / `Integration ... budget exhausted` / `E2E passed after 8/10`。

深层原因有三层：
1. Unit 层的失败（authService 24 failed，行 1797+）其实是**实现还没写完**的正常 RED——TDD 循环本来就要修，这部分不是浪费；
2. Integration 的红全挂在 E2 同一个 SPA fallback 缺陷上（`app.test.js` 10 轮 × ~30s 重跑）；
3. 20:18:41/20:20:11（行 6840s）两次对已关闭的 Unit 层发起 `run_tests test_type=Unit` 被拒（"The active TDD layer is E2E"）——agent 不确定早层是否真的通过，浪费 2 次调用+思考时间。**层状态对 agent 不可见是已知缺口**（tdd-retry-handoff PR #58 一族）。

注意 agent 最终在 20:20:13 输出了 `IMPLEMENTED`（行 6878），但框架判定 Unit/Integration 预算耗尽而判负——这是"agent 认为完成 vs 预算计数判负"的错位：10 次预算里有一部分花在了"修同一个产品 bug 的重复重跑"上。

### E4【P1·次级】工具层错误共 19 起（runner-events 权威口径），全部自愈

| 类别 | 数量 | 代表 |
| --- | --- | --- |
| read_file 权限拒绝 | 8 | `/frontend/src/App.tsx`（无 /workspace 前缀，行 66）、`/workspace/frontend/dist/index.html`（dist 在拒绝清单）、`node_modules/bcrypt/package.json`、offset 超文件长度 3 起 |
| glob/ls 拒绝 | 4 | `glob /`（根路径锚点被拒，已知 family）、`ls /workspace/frontend/dist` |
| write/edit 阻塞 | 5 | 4× DESIGN 期"Repeated write blocked"（RegisterPage.tsx 等，行 749 拦截后 agent 改用 append 策略成功）、1× delete blocked |
| write_file error | 1 | `/tmp/express-wildcard-test.js`（临时诊断脚本路径被拒，agent 改写到 /workspace 下成功） |
| edit_file error | 1 | old_string 不匹配 |

无死循环、无 GraphRecursionError、无 429/网络错误、无 install/build 门禁失败。19:49:19 两起 green-baseline 拒绝（行 1438）是**门禁正常工作**（2 个测试文件实现前就绿，被要求返工），不是错误。

### E5【P2·观察】auto-TDD-retry 注入后 agent 的第一反应是重读 skill 和代码

尝试 2 的前 1.5 分钟（20:20:31–20:22:00）agent 读了 tdd-test-failure-repair skill（第 2 次）、重读 app.js/RegisterPage 等。重复读取统计：auth-session-consistency 4 次、web-test-harness 2 次、tdd-test-failure-repair 2 次。每次 skill 全文注入 ~3-12K tokens（行 1656/4830 的 result_chars 可见单次 3,164+）。

---

## 4. 耗时优化点排序（按可回收分钟数）

| # | 问题 | 实测成本 | 根因落点 | 预期回收 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 1 | **依赖门禁串行**：REQ-2:DESIGN 等 REQ-1:IMPLEMENT 完成才放行，19:51:03→20:27:59 全程 36.9 min 无 REQ-2 工作 | 36.9 min 槽位空转（若 REQ-2 DESIGN+IMPLEMENT 能并行，按 REQ-1 的 22.7 min 体量估可省 ~20 min 墙钟） | `core/workflow.py` `_task_dependencies_met`（现 main :1764）DESIGN 分支要求依赖 IMPLEMENT 完成 | ~20 min | **Issue #83 已开票**（依赖 DESIGN 完成即放行 + 契约漂移校验；ADR 0001 杠杆②） |
| 2 | **SPA fallback 缺陷死磕**（E2）：Integration 10 轮重跑 + E2E 5 次全量重跑 + 7 次后端 teardown/重启 | 尝试 1 的 29.4 min 里 ~22 min 花在定位+修复；对应 $7.03 的大部分 | 模板 dist 解析（template_patches.py） | 一次性修复后整个 family 消失；本 run 可省 ~25 min（含尝试 2 的 7.5 min 大部分） | **未修，建议开票** |
| 3 | **E2E 全量重启**：每次 Playwright 调用 teardown 后端再冷启动（7 次中途 teardown），单次调用 131–148s，其中真正跑测试约 30s | 5×~120s ≈ 10 min | E2E runtime 生命周期管理（会话级复用机制存在但本次 run 内未生效于失败重试场景） | ~8 min | 部分已知（E2E session reuse PR #15 相关） |
| 4 | **run_tests 间隙的 LLM 思考**：32 次调用之间 759s 纯间隙（读日志/想对策/改文件） | 12.7 min | TDD 失败上下文交接效率（fingerprint digest PR #59/#63 在本次运行代码中已含，仍不够） | ~5 min | 部分已在 PR #46/#47/#58/#63 持续治理 |
| 5 | **skill 重复读取**：attempt 2 重读 3 个 skill | ~1.5 min + ~20K tokens | retry 注入提示可引用已有读取（#66 skill catalog 机制已合入，本次运行未含） | ~1 min | 已被 PR #66 方向覆盖 |

**不构成问题的部分**（防止过度归因）：DESIGN 阶段 15.2 min 产出了 22 个接口契约 + 8 个测试清单（interfaces.json/tests.json），无返工记录，是有效工作；缓存命中率 ~20 倍 input 说明上下文缓存策略工作良好；零 API 重试说明网关/流式配置健康（PR #44/#76 修复后首次实测干净）。

## 5. 与前一 run（arc-output，17:08 结束）的对比

同一需求的上一 run：REQ-1:IMPLEMENT **FAILED**（未触发 auto-TDD-retry？），REQ-2/ROOT 同样 BLOCKED_BY_DEPENDENCY 收场。本 run 把 REQ-1 推到了 PASSED（auto-TDD-retry 生效），说明 PR #55 合入的 baseline 门禁 + auto-retry 机制方向有效；差别只剩 E1 的释放缺口——与 PR #77 修的正是同一场景（test1 arc-output5 的复刻）。

## 6. 本次产物的续跑方案（当前 main，已验证语义）

```bash
cd D:\code\arc-agent
python arc_main.py compile D:\code\simple-ticketing\requirements -o D:\code\simple-ticketing\arc-output1 -t web --port 3301 --resume --retry-failed
```

依据（现 main 代码）：`_apply_retry_plan`（core/workflow.py:1871）会把 `NODE_BLOCKED_BY_DEPENDENCY` 的 REQ-2/ROOT 一并纳入重置 → `_reset_node_for_retry` → `_release_dependency_blocks`（:730）解除；REQ-1 已 PASSED 不受影响；REQ-1:IMPLEMENT 已 COMPLETED 使依赖门禁满足，REQ-2:DESIGN 立即变为可调度。REQ-2 DESIGN/IMPLEMENT 会从 REQ-1 已合并的 integration HEAD 增量出发。预计增量成本 = REQ-2 两个阶段 + ROOT:IMPLEMENT ≈ $4–6 / 20–30 min（按 REQ-1 体量折半估）。

## 7. 附录：数据再 derivation 脚本

时间线/槽位/run_tests 序列：正则 `^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})` 解析 debug.log；token/成本：`json.loads` runner-events.jsonl 按 `type`/`node_id`/`phase` 聚合 `usage`/`latency`/`cost`；尝试 1/2 切分点：UTC 时间戳 12:20:30。工具错误：`tool_usage.status != 'ok'`（19 起）+ `tool-result> ... Error:` 文本扫描交叉核对。

关键行号索引：65（ROOT:DESIGN read 拒绝）·749（DESIGN 拦截）·1438（green-baseline 拒绝）·1797+（Unit RED 明细）·3379/3510/...（Integration SPA fallback 10 轮）·5754/5920/6112/6224（dist 修复路径）·6910–6915（失败→BLOCKED→retry）·8593（终局）。
