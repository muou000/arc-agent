# simple-keep 全量验收 run 的模型基线折算（issue #84）

日期：2026-09-21。状态：**基线已折算，run 待预算确认**（issue #84 要求跑之前把预算与时长估给用户确认）。

## 0. 一句话结论

任务单位模型下，旧默认调度（depth-1 亲和 + 依赖门禁全等 IMPLEMENT）的关键路径是 **72/90 任务单位**；
验收配置（`ARC_AFFINITY_DEPTH=2` + 门禁流水线化 + 仲裁）的关键路径是 **28–38 单位**（流水线开启时降到纯结构地板 28）。
按历史单价 t_unit ≈ 8–14 min/单位，**模型基线 ≈ 9.6–16.8 h**，验收配置预测墙钟 **≈ 4–9 h**（3 slots 下吞吐地板 30 单位），
预测降幅 47–61%，**满足 ≥30% 验收线且有余量**。预估成本 **$90–220**（MiniMax-M3，96% 缓存命中实测口径）。

## 1. 方法

任务单位模型（2026-09-20 并行化诊断定案，等时长假设）：一个 DESIGN 或 IMPLEMENT 任务 = 1 单位，
关键路径长度（最长依赖+结构+亲和串行链）× 单位时长 = 模型墙钟。**不做 $200+ 的双跑基线**（ADR 0001 验证协议）。

关键路径用**真实调度器地图**计算，不是手工重推：`records/simple-keep-baseline-model-2026-09-21.py`
调用 `ARCWorkflowManager._build_processing_tasks / _build_parents_map / _build_descendants_map /
_build_affinity_map / _build_dependencies_map / _drop_ancestor_dependency_edges / _break_dependency_cycles`
构造任务图（结构边 + 声明依赖过滤后生效边 + 同亲和组 flat 顺序串行链），再求最长路径。
边语义与 `core/workflow.py` 的 `_task_dependencies_met` 一致：

- 自身：D:x → I:x；父子：D:parent → D:child；后代：I:descendant → I:node；
- 声明依赖（祖先-后代边按队列规则丢弃后无剩余丢弃边）：默认门禁等依赖 IMPLEMENT；流水线模式 DESIGN 等依赖 DESIGN；
- 亲和组：同组任务按 flat 顺序串行（复用同一 worktree 目录）。

## 2. 计算结果（45 节点 / 90 任务）

| 场景 | 关键路径 | 组数 | 最大组 |
| --- | --- | --- | --- |
| 结构+依赖边地板（无亲和串行） | **28**/90 | 7 | 60 |
| 旧默认（depth-1 亲和，门禁关） | **72**/90 | 7 | 60 |
| depth-1 亲和 + 门禁流水线 | 64/90 | 7 | 60 |
| depth-2 亲和，门禁关 | 38/90 | 24 | 20 |
| **验收配置（depth-2 亲和 + 门禁流水线）** | **28**/90 | 24 | 20 |

- 地板 28 与 2026-09-20 诊断手估值 28 一致（交叉验证）；旧默认 72 与诊断手估 ~74-75 同量级（手估含更粗的合并等待假设）。
- depth-2 把 REQ-2 的 60 任务大组拆成 24 组（最大组 = REQ-2.7 子树 10 节点 20 任务）。
- 流水线开启后验收配置的关键路径 = 纯结构地板 28：声明依赖在关键路径上的等待被完全消除。
- 3 slots 的吞吐地板 = ⌈90/3⌉ = 30 单位 > 28，故 3 slots 下实际墙钟由 max(关键路径, 吞吐) ≈ **30–38 单位** 决定；
  提槽到 4+ 只在冲突率数据支持时考虑（ADR 0001：先 3 slot 实测）。

## 3. 历史单价（t_unit 与 token）

| 运行 | 树规模 | 墙钟/任务 | 成本/任务 | 口径 |
| --- | --- | --- | --- | --- |
| test1 arc-output4（2026-09-20，MiniMax-M3，3 节点 6 任务） | 6 任务 | 8.35 min | $6.3（估算口径，input 高估 ~4x，修正后 ≈$1.7–2） | 全绿 |
| easy-ticketbooking（2026-09-21，MiniMax-M3，3 节点 6 任务，#95 后 main） | 6 任务 | 14.4 min | **$3.48（实测，96% 缓存命中）** | 全绿含一次 TDD 重试 |
| 12306 有效窗口（2026-09-13，并发 4） | 26 结算任务 | 7.3 min | ¥1.53 | 额度截断窗口，取下限参考 |

取 **t_unit ≈ 8–14 min/单位、$2–3.5/真实任务**（keep 叶子特征比 ticketbooking 小、比 12306 截断窗口完整）。

## 4. 验收折算

- **模型基线**（旧默认）= 72 单位 × 8–14 min ≈ **9.6–16.8 h**（这正是双跑基线被否决的原因）。
- **验收配置预测** = max(关键路径 28, 吞吐 30)–38 单位 × 8–14 min ≈ **4–8.9 h**；另加固定开销：
  脚手架+视觉预计算 ~6–11 min、每任务合并（含健康门禁探活，秒级）、冲突重排/仲裁（预算 1 次/节点，罕见）。
- **≥30% 验收线** ⇔ 实测墙钟 ≤ 0.7 × 72 = 50.4 单位 × t_unit。预测 30–38 单位，余量 ~25–40%。
  未达标时按 issue 要求留档差距与瓶颈分解，不粉饰。
- **成本预测** = 64 个真实叶任务（26 个 folder 任务走无模型跳过路径）× $2–3.5 ≈ **$130–220**；
  按 12306 的低单价锚（$0.56/M blended × 2.6–6.2M tokens/任务）下限 **$90**。预估 **$90–220，中位 ~$150**。
  模型：MiniMax-M3（当前 `.env`），chat_completions。

## 5. 运行计划（待确认）

```bash
# 前置：PR #118（#91 竞态修复）合并后再跑，否则 2+ slots 必现幽灵删除
export ARC_AFFINITY_DEPTH=2 ARC_DESIGN_GATE_PIPELINE=1 ARC_MERGE_ARBITRATION=1
export ARC_MAX_CONCURRENT_TASKS=3
python arc_main.py compile <arc-bench-test/keep/requirements> -o <C:\...\arc-keep-full> -t web --port 3301 --clean
```

输出放 C 盘（D 盘对 npm 类负载慢 7–13 倍，见 12306 报告 §5.8）。指标从 `.arc/runner-events.jsonl`
（llm_usage / requirement_state / 合并事件）与 `processing_queue.json` 聚合；
外部 33 个 spec（`arc-bench-test/keep/tests/`）作为最终裁判跑一遍，得分与本表一并留档 `records/` 并回链 #84。

## 6. 验收清单对照（issue #84）

- [x] 模型基线折算留档（本文档 + 脚本；方法与数字见 §1–§4）
- [ ] 全量 run 完成，队列无终局 BLOCKED/FAILED（待预算确认后执行）
- [ ] wall-clock 相对基线降幅 ≥30%（对照线：≤ 50.4 单位 × t_unit；未达标留档分解）
- [ ] 仲裁升级率/冲突率/token 实测数据入 `records/`
- [ ] 外部测试 spec 得分留档
- [ ] 结果与结论写入 `records/` 并回链 #84
