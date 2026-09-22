# 合并落地不打断执行任务:agent 下一文件工具边界即重放,冲突强制消解

状态:**已实施,默认关闭**——机制由 `ARC_REBASE_ON_MERGE` 门控(默认关闭),实施拆解见 issue #127;回归测试见 `tests/test_workflow/test_rebase_on_merge.py`。mini benchmark(2 节点开/关各一次,懒式版本)已运行,见 `records/evals/rebase-on-merge-2node-run2/report.md`;饿式改造后的正向路径证据待 keep 规模 run。

背景:跨组合并冲突走到重排轨道时,已完成阶段的产物被整段丢弃——DESIGN 冲突连设计产物一起清空、IMPLEMENT 冲突至少重跑实现(`reset_branch_to_integration` 路径),这是任务单位浪费的直接来源之一;同时执行中任务读到的集成面可能已过时,语义漂移靠下游 TDD 红灯兜底。决定:兄弟合并落地时给执行中任务挂一条**未应用合并**(Pending Merge,变更文件集);执行 agent 的**下一次任意文件工具调用**(read/edit/write/append/delete,无论触碰哪个路径)在工具边界静止点先做 WIP commit(阶段中途无既有 commit,脏树必须先落)再 rebase 到最新 integration HEAD,然后服务本次调用并注入变更清单——这是**饿式**触发(2026-09-22 修订):合并落地即对齐,不等待(也不依赖)agent 恰好触碰变更路径。冲突以标记落文件、连同对面契约卡交执行中 agent 消解;消解是**强制**的——标记未清期间,冲突文件集之外的文件调用被拒绝(冲突集自身保持可写,消解编辑在自身的边界完成 rebase);软护栏:同一阶段累计 3 次*实际执行过 git 动作*的冲突重放后(被动观察不计数),停用该阶段剩余时间的重放并 abort 悬挂中的 rebase。git 机械操作全部系统侧,agent 无 shell、无 git,只做内容级冲突消解——这是硬边界。全程 fail-open:机械失败静默跳过、恢复重放前状态、落回既有轨道(机械消解/仲裁/重排);不新增终态、不改既有轨道,重排/仲裁预算与之独立互不消费;rebase 的 git 段持 `integration_gate` reader;WIP commit 落节点分支(非 stash),阶段末 integrate 照常合并。配套:模板补丁把 Playwright 易变产物(test-results/、playwright-report/)加入 backend .gitignore,使其不进检查点、合并与重放(锁文件风险的预防)。

## Considered Options

- 懒式触发(仅当 agent 的文件工具触碰变更文件集内路径才重放):2026-09-21 grill 定稿的原方案。修订时被否决,原因有二:其一,mini benchmark(2 节点)显示「写完即不再触碰」是真实高发形态——B 在 A 合并前写完共享文件,合并落地后 B 再不触碰它们,懒式永不触发,重叠原样落到阶段末合并轨道(该次 run 即 4 文件冲突 + 健康门禁失败 + 仲裁失败 + 节点判负);其二,触发语义依赖触碰巧合而非集成面新鲜度本身,覆盖范围是偶然而非设计。饿式的对齐成本(无关文件调用也触发一次重放)很低:一次重放消费全部 pending(无重放风暴),同步 git 开销秒级。
- agent 持 git(通用 shell 或白名单 git 工具,agent 自行 rebase):否决。路径权限、stage discipline、file claim、manifest 锁只在 typed 工具调用上强制,shell 一开全部失守;agent 持 rebase 的最坏情形(reset --hard 自毁阶段成果)比现状更糟;给 agent 新失败空间有烧 token 先例(run7 offset 探测、300 步循环)。
- 阶段末消解(阶段结束 integrate 撞冲突后经 checkpointer 重新 ainvoke 消解):零 mid-phase 风险,但放弃 mid-phase 语义新鲜度,且现有重排/仲裁轨道已覆盖该形态。
- 维持现状:重排即"事后丢弃式重放",基线。

## Consequences

- 语义漂移仍有边界:合并落地前 agent 已读入上下文的内容,重放本身不重写 agent 记忆——变更清单注入提示 agent 重读依赖文件,但不强制;grep/glob 扫过陈旧内容不触发(它们不是文件路径工具);从未声明依赖的漂移是依赖门禁 + 接口卡 + TDD 的领地——本机制明确不背此锅。
- 与既有轨道的关系:只减少进入合并层冲突轨道的次数,不改变轨道本身,不新增终态;重排/仲裁预算与之独立、互不消费。
- 饿式无重放风暴:一次重放消费全部 pending,同一波合并至多触发一次(下一次文件工具边界);非文件工具(验证工具 run_tests/run_build、清单工具)不触发重放,但强制消解期间验证工具保持可用。
- 强制消解的边界:阻塞只作用于文件工具且只在冲突标记未清期间;若 agent 的消解陷入循环,软护栏(3 次后停用 + abort)把重叠交还合并轨道;消解失败不是新终态,走既有失败轨道。
- 需同步维护:`core/worktree.py`(WIP commit + 重放机械段,持 reader 门,遵守 #91 读写门语义)、`agents/runtime/rebase_gate.py`(饿式触发、强制消解门、软护栏)、middleware 工具边界拦截与工具结果注入、runner 事件(`rebase_replay`:started/resolved/conflicts/aborted)、真实 git 回归测试(AGENTS.md「工作流和队列」)。
