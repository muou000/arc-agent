# ARC_REBASE_ON_MERGE mini benchmark（issue #127 验收）

- 日期：2026-09-22
- 代码：`feat/rebase-on-merge`（f73ca1f+b659edb，最终形态含评审修复）
- fixture：`reb2`（ROOT/REQ-A.1/REQ-B.1 两个叶子子树，各自做 DB+API+UI 列表功能，共享 HomePage/app.js/init_db 表面）
- 运行：A/B 各一次（eval harness，`--baseline-env ARC_REBASE_ON_MERGE=0` vs `--candidate-env ARC_REBASE_ON_MERGE=1`）
- 工件：`records/evals/rebase-on-merge-2node-run2/`（run1 是 NameError 事故，保留于 `records/evals/rebase-on-merge-2node/`，见下）

## 结果

| 指标 | rebase-off (baseline) | rebase-on (candidate) | 差 |
|---|---|---|---|
| Pass rate | 100%（1/1） | 0%（REQ-B.1 design 失败） | -100pp |
| Tokens | 9,024,136 | 4,821,966 | -4.2M* |
| Cache hit | 92.6% | 90.1% | -2.5pp* |
| 时长 | 1652s | 1407s | -245s* |
| 估成本 | ¥8.17 | ¥6.37 | -¥1.80* |

*带星号指标不可比：candidate 因 REQ-B.1 失败提前终止，token/时长天然偏低。

## 判读

1. **重放从未触发**：candidate 的 runner 事件中 `rebase_replay` 为 0 条。REQ-A.1 与 REQ-B.1 的 DESIGN 相继完成，两个 agent 对共享表面的触碰在时间上不重叠（REQ-A.1 已合并后 REQ-B.1 才写 HomePage/app.js），懒式触发没有命中窗口。这符合 ADR 0003 的边界声明：「grep/glob 扫过陈旧内容不触发；从未触碰的漂移是合并轨道的领地」。
2. **candidate 的失败与重放无关**：REQ-B.1 的 DESIGN 合并走了既有合并轨道——4 个共享文件的纯追加机械消解（app.js/seed_db.js/HomePage.tsx/.last-run.json）后，**合并后健康门禁失败**（merged workspace 后端起不来），LLM 仲裁（2 次触发均记录）未能修复，按既有轨道终判失败。这是与本特性 flag 无关的合并轨道偶发（baseline 同样存在 2 次 merge_arbitration 记录，只是它的健康门禁通过）。
3. **REQ-A.1 在 candidate 全绿**（Unit/Integration/E2E 全过）——特性开启下并行执行、门禁、合并无回归。
4. **run1 事故（已修复）**：首跑 candidate 28s 崩溃，`NameError: name 'node_id' is not defined`——`_build_task_phase_runner` 的 provider 闭包引用了任务作用域外的名字。已修复（`handle.node_id`）并加回归测试 `test_task_runner_gate_provider_builds_a_middleware` 钉住；该 bug 单测层不可见（闭包只在真实 agent 构建路径执行），mini benchmark 是唯一暴露面，这本身印证了验收标准要求 benchmark 的价值。

## 决策（据此回答验收标准的两个问题）

- **是否翻默认**：**不翻**。默认保持关闭。理由：(a) 本 fixture 上懒式触发 0 命中，开/关行为等价（直通无回归是下限证据，不是收益证据）；(b) candidate 的失败虽与重放无关，但缺少一次"触发路径在真实 run 中成功重放"的正向证据；(c) 触发窗口依赖共享文件的触碰时间重叠，真实基准树（simple-keep 等）中才会出现。
- **是否补饿式触发**：**暂不**。ADR 0003 已把饿式列为「mini benchmark 显示剩余浪费显著再评估」的杠杆；本次运行没有产生该证据（重放零触发，无从量化 mid-phase 语义新鲜度的收益）。留待 keep 全量 run。

## 未运行的验证

- 未做 repetitions>1 的统计对比（单对运行，pass/fail 与 token 差异不构成显著性）。
- 未在会真实触发重叠触碰的树上（如 simple-keep REQ-2 深子树 + ARC_AFFINITY_DEPTH=2）验证重放的正向路径；回归测试（`tests/test_workflow/test_rebase_on_merge.py` 26 个真实 git 用例）覆盖了机械与边界行为。
