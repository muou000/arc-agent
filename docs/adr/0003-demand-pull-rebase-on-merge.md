# 合并落地不打断执行任务:agent 触碰未应用合并文件时,才在工具边界按需重放

状态:**设计定稿,未实施**——机制由 `ARC_REBASE_ON_MERGE` 门控(默认关闭),实施拆解见 issue #127;回归测试与 mini benchmark 通过前不翻默认。

背景:跨组合并冲突走到重排轨道时,已完成阶段的产物被整段丢弃——DESIGN 冲突连设计产物一起清空、IMPLEMENT 冲突至少重跑实现(`reset_branch_to_integration` 路径),这是任务单位浪费的直接来源之一;同时执行中任务读到的集成面可能已过时,语义漂移靠下游 TDD 红灯兜底。决定:兄弟合并落地时只给执行中任务挂一条**未应用合并**(Pending Merge,变更文件集),不主动打断;agent 的文件工具调用(read/edit/write/delete)触碰该集合内路径时,middleware 在该**工具边界静止点**先做 WIP commit(阶段中途无既有 commit,脏树必须先落)再 rebase 到最新 integration HEAD,然后服务本次调用并注入变更清单;冲突以标记落文件、连同对面契约卡交执行中 agent 消解(允许反复;软护栏:同一阶段连续 3 次携带冲突的重放后,停用该阶段剩余时间的 mid-phase 重放),消解后继续原阶段。git 机械操作全部系统侧,agent 无 shell、无 git,只做内容级冲突消解——这是硬边界。全程 fail-open:机械失败(含 Windows 残留 dev server 锁文件)静默跳过落回既有轨道;rebase 的 git 段持 `integration_gate` reader;WIP commit 落节点分支(非 stash),阶段末 integrate 照常合并。

## Considered Options

- 饿式双通道(合并落地时按冲突域交集主动重放):多覆盖"已写完且不再触碰的文件被兄弟改掉"一类冲突的提前暴露与陈旧读告警;但该类冲突由合并层仲裁已可保留阶段产物,eager 边际价值不明,mid-phase 停顿与文件锁暴露面更大。留作未来杠杆,mini benchmark 显示剩余浪费显著再评估。
- agent 持 git(通用 shell 或白名单 git 工具,agent 自行 rebase):否决。路径权限、stage discipline、file claim、manifest 锁只在 typed 工具调用上强制,shell 一开全部失守;agent 持 rebase 的最坏情形(reset --hard 自毁阶段成果)比现状更糟;给 agent 新失败空间有烧 token 先例(run7 offset 探测、300 步循环)。
- 阶段末消解(阶段结束 integrate 撞冲突后经 checkpointer 重新 ainvoke 消解):零 mid-phase 风险,但放弃 mid-phase 语义新鲜度,且现有重排/仲裁轨道已覆盖该形态。
- 维持现状:重排即"事后丢弃式重放",基线。

## Consequences

- 语义漂移仍有边界:合并落地前读过的文件,落地后 agent 记忆过时,懒式不发告警(除非再次触碰);grep/glob 扫过陈旧内容不触发;从未触碰但基于父契约卡/设计文档依赖其内容的漂移,是依赖门禁 + 接口卡 + TDD 的领地——本机制明确不背此锅。
- 与既有轨道的关系:只减少进入合并层冲突轨道的次数,不改变轨道本身,不新增终态;重排/仲裁预算与之独立、互不消费。
- 懒式无重放风暴:一次重放消费全部 pending,同一波合并至多触发一次;触碰检查是每次文件工具调用上路径对小集合的匹配。WIP commit 使节点分支含 `wip:` 提交,审计可辨。
- 需同步维护:`core/worktree.py`(WIP commit + 重放机械段,持 reader gate,遵守 #91 读写门语义)、middleware 工具边界拦截与工具结果注入(复用 `_annotate_pending_contract` 形态)、真实 git 回归测试(AGENTS.md「工作流和队列」);runner 事件新增重放生命周期字段。
