# ADR 0007: manifest 字段处置阶梯与 coverage_scope 默认值

日期：2026-09-24　|　状态：已接受　|　来源：#233（#230 serial-5 事故的审计拆分票）

## 背景

`DesignArtifactRegistry.prepare_tests` 对测试 manifest 的四个字段级分支（`type` 为空、`file_path` 缺失/路径非法、`coverage_scope` 非法、重复 test_id）一律抛 ValueError 判死整个 DESIGN 阶段；非 dict 行与空 `test_id` 行则被静默丢弃。同一阶段里，接口条目的空 `interface_id` 在 #230 已改为"丢弃 + 可观察告警"，`type` 已有"回填阶梯 + 一次 repair"通道——测试 manifest 是同一形态却无对应处置。同时 `InterfaceDesignResponse.interfaces` 是松散 `list[dict]`，`type` 缺失要等到注册期才暴露（serial-5：11 条全部缺 `type`，31 分钟产出作废）。

## 决策

1. **测试 manifest 字段按三级阶梯处置**（`core/design_artifacts.py` `prepare_tests`）：
   - **回填**：有确定性来源的字段。空 `test_id` → `mechanical_test_id(node_id, file_path)`（与 reconcile 重新挂载同一生成器）；`coverage_scope` 缺失 → `owned`（见下）。声明层的 reconcile 先行：响应行丢了 `type`/丢了或弄坏 `coverage_scope` 时从锁定声明恢复（`backfilled_fields` 上报）。因此注册层"保持判负"的空 `type` 指的是**声明恢复之后**仍无值——它没有越过任何可用来源。
   - **丢弃 + 可观察**：无法登记的行（非 dict、无 `file_path`）→ `on_dropped_entry(item, reason)` 回调，phase 层发 warning。不判死——owned-witness 门与 foreign-owned 检查仍对剩余 manifest 响亮把关。
   - **保持判负**：没有确定性来源、且静默放行会掩盖真实契约破坏的分支——空 `type`、app-type 路径非法、非空非法 `coverage_scope`、重复 `test_id`。声明工具的 re-declare 循环已给过模型带规则原文的修正机会，到注册层仍是坏值即漂移。
2. **`coverage_scope` 缺失默认 `owned`**（语义决策）：`owned` 是保守方向——被错误按 owned 处理的测试仍受基线 RED 门（必须先红）和 foreign-owned 检查约束，两类失配都响亮失败；默认成 `dependency`/`shared` 则静默豁免 witness，把质量裁剪藏进基线。decode（`TestManifestItem` default）与声明（`normalize_coverage_scope`）两层早已建立同一默认，注册层拒绝缺失值是纯不对称。
3. **DESIGN 响应 schema 收紧到解码期**：`interfaces` 改具名子模型 `InterfaceContractRecord`——`interface_id`（min_length=1）与 `type: Literal["UI","API","FUNC","DB"]` 必填，解码即拒绝；其余字段可选、`extra="allow"` 保底透传（存储契约内容不得比松散 dict 时代丢失字段）；`inputs`/`outputs`/`test_focus` 无统一形状，保持 `Any`。#230 的注册期回填阶梯保留，作为绕过解码的恢复通道（裸 JSON 尾信、围栏 prose、机械记录）的机械兜底。

## 备选与取舍

- 重复 test_id 机械改名/去重：被否——test_id 是追溯身份，机械改名凭空发明身份，任意去重可能静默丢掉真实测试的登记；保持判负并让 in-session 解码重试（具名子模型已要求 test_id）拦截绝大多数。
- coverage_scope 非法值也回填 owned：被否——"填错"不是"没填"，回填错误值会教模型精度无所谓；reconcile 层已用声明行的合法值把可救的漂移救回。
- 子模型关闭 extra（`additionalProperties: false` 全量具名）：被否——模型自创字段（历史 run 中 `mount_path`、`columns` 等形状）会静默丢失，违反契约保真。

## 验证

- faux 钉子：解码拒绝缺/坏 `type`（先红后绿见 `tests/test_agents/test_interface_contract_record_schema.py`）、回填/丢弃/判负三分支（`test_design_artifact_registry.py`）、声明层恢复（`test_test_manifest_lock.py`）、phase 层告警接线（`test_design_type_backfill_phase.py`）。
- 真实端点对照（glm-5.3-flash / arc-bench 网关，主 pass 实际走 ToolStrategy 工具调用解码）：松散 schema 臂模型自行返回 `type='ui_component'/'db_table'`、`path` 字段——正是 serial-5 缺陷类；收紧 schema 臂两条记录解码为规范 `UI`/`DB` + 正确 `file_path`。结论记于 #233。
- 追溯七表结构不变；事件流无新增类型（复用 log warning 通道）。

## 已知边界

注册层的可观察性只覆盖"解析到任何时刻都不存在的引用"；**前向引用**（指向晚于本节点注册的接口）在注册时既不建边也不告警，且后续节点注册时不回算——这条缺口不在本 ADR 的处置阶梯内，跟踪于 #238（悬挂引用收尾清扫）。
