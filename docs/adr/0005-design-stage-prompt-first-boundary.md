# DESIGN 仅设计的行为边界：提示词机械化优先，行数闸撤除，翻转语义为条件回退

状态：**定案**——提示词与护栏改造随实施票落地；本文记录 2026-09-22 easy-ticketbooking `arc-output-serial` 串行 run 复盘后的 grill 定案（前提：不引入并行化）。

## 背景

串行 run（3 节点 6 任务全绿，77.4min/$13.15，MiniMax-M3）显示 DESIGN 阶段系统性越权实现：InterfaceDesigner 写出 142 行完整 `account_service.js`、含 SQL 的 `account_repository.js`、完整 React 页面与 4 个测试文件（REQ-1:DESIGN 22min；8 次写护栏拒绝后模型用拆块 + `append_file` 全部绕过落盘；DESIGN 输出 token 197K，占全场输出 76%）。

根因三层：

1. **提示词自相矛盾**。`agents/context/prompts/interface_designer.py` 三处明令禁止实现（Hard boundary 段、L51 "must not contain a feature-complete business flow"、L98 "Do not turn DESIGN into an implementation pass"），但 L52/L53/L96 三处教"compact first chunk + `append_file` 拆块续写 + 用拆块克服骨架限制"。运行日志中模型被 160 行限制拒绝后的对策自述是 "account_service.js: smaller skeleton"——拆小块而非缩边界，与拆块教学一致。
2. **骨架定义是程度词**。"feature-complete 与否"无法机械判定，模型自我裁量空间大；操作指南（拆块配方）具体、禁令（不实现）模糊时，模型遵循具体的那个。
3. **拒绝后无出路**。行为逻辑必须落在某处，提示词未提供合法去处，护栏拒绝只制造返工循环（拒绝→拆块→再写）——这正是 token 浪费的发生机制，而不是浪费的治理。

## 定案

1. **提示词机械化**（主防线，PR #74 read-lock 验证过的"显著性 + 封逃逸姿势"模式）：
   - 骨架定义改为形状判定：无函数体——只有 imports、类型/常量定义、带类型签名的导出、路由表到处理器名的映射、`// TODO(TDD): <行为>` 标记；出现 if/循环/SQL/校验逻辑或超过单条 return 的函数体即为实现。
   - 删除 L52/L53/L96 拆块教学，替换为显式出路："装不进一次紧凑写入的内容不是骨架——把完整行为描述写进阶段响应交给 TestDrivenDeveloper。"
   - 补经济动机：DESIGN 写实现是弃置功（TDD 须对照测试重推重验）；契约在此设计一次，行为在 TDD 实现一次。
   - Hard boundary 前置到 Role 段最前。
2. **撤除 `_MAX_SKELETON_LINES=160` 单次行数闸**（`agents/runtime/stage_discipline.py`）：行数闸是本次返工循环的第一推动力。主防线改为骨架形状定义 + business-mutation 嗅探 + 出路文案。嗅探保留（本次拦下过 SQL repository），拒绝文案改为指向出路。
3. **翻转语义（承认骨架即实现起点，TDD 增量验证）作为条件回退**，不立即实施。

## 否决的替代

- **行数按文件累计**（覆盖 append/拆块）：把返工循环从单次写扩大到整个阶段，与"消 token 浪费"的目标相反。
- **路径硬白名单**：骨架与实现在同一路径上无法用路径区分，且会误伤测试资产——测试资产在 DESIGN 产出是现状契约（`declare_test_manifest` 在 DESIGN 调用）。
- **立即翻转语义**：提示词机制与先例支持先给模型自发约束的机会；且本次 run 中 TDD 并未重写 DESIGN 写的实现（代码基本正确，真实浪费在护栏缠斗与测试文件缺陷），说明"提示词成功后的世界"与"翻转后的世界"差距有限，先试便宜的一侧。

## 回退触发条件

C 票验收 = 2 节点 mini benchmark 有界实测（~$5 量级）：DESIGN 写出文件普遍无函数体 + 端到端仍全绿 + 总时长/成本不升。若实测 DESIGN 越权仍显著（写出文件普遍含函数体），开翻转票：撤销"DESIGN 不实现"约束，TDD 改为从 DESIGN 产物增量验证，消除"写了又不认"的组合。
