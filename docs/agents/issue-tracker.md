# Issue 跟踪：GitHub

本仓库的 issue 和 spec 以 GitHub Issues 形式存在（`muou000/arc-agent`）。所有操作使用 `gh` CLI。

## 约定

- **创建 issue**：`gh issue create --title "..." --body "..."`。多行正文用 heredoc。
- **读取 issue**：`gh issue view <number> --comments`，用 `jq` 过滤评论并附带读取标签。
- **列出 issue**：`gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`，按需加 `--label` 和 `--state` 过滤。
- **评论**：`gh issue comment <number> --body "..."`
- **加 / 删标签**：`gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **关闭**：`gh issue close <number> --comment "..."`

在 clone 内运行时 `gh` 自动从 `git remote -v` 推断仓库，无需显式指定。

## Pull request 作为 triage 面

**外部 PR 作为请求面：否。** _（若本仓库开始把外部 PR 当作功能请求处理，把此值改为 `yes`；`/triage` 会读取此开关。）_

设为 `yes` 时，PR 走与 issue 相同的标签和状态，使用 `gh pr` 等价命令：

- **读取 PR**：`gh pr view <number> --comments` 和 `gh pr diff <number>`。
- **列出待 triage 的外部 PR**：`gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments`，只保留 `authorAssociation` 为 `CONTRIBUTOR`、`FIRST_TIME_CONTRIBUTOR` 或 `NONE` 的（丢弃 `OWNER`/`MEMBER`/`COLLABORATOR`）。
- **评论 / 标签 / 关闭**：`gh pr comment`、`gh pr edit --add-label`/`--remove-label`、`gh pr close`。

GitHub 的 issue 和 PR 共用一个编号空间，裸 `#42` 可能是两者之一：先 `gh pr view 42`，失败再 `gh issue view 42`。

## 当 skill 说「发布到 issue tracker」

创建一个 GitHub issue。

## 当 skill 说「拉取相关 ticket」

运行 `gh issue view <number> --comments`。

## Wayfinder 操作

供 `/wayfinder` 使用。**地图（map）** 是一个带 `wayfinder:map` 标签的 issue，**子 ticket** 是它的 child issue。

- **地图**：单一 issue，标签 `wayfinder:map`，正文承载 Notes / Decisions-so-far / Fog。`gh issue create --label wayfinder:map`。
- **子 ticket**：以 GitHub sub-issue 形式挂到地图上（对 sub-issues 端点调 `gh api`）。sub-issue 不可用时，在地图正文加 task list 并在子 ticket 正文顶部写 `Part of #<map>`。标签：`wayfinder:<type>`（`research`/`prototype`/`grilling`/`task`）。被认领后 assign 给驱动的 dev。
- **阻塞关系**：GitHub **原生 issue dependencies**，这是 UI 可见的权威表示。加边：`gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`，其中 `<blocker-db-id>` 是阻塞方的数字 **database id**（`gh api repos/<owner>/<repo>/issues/<n> --jq .id`，_不是_ `#number` 也不是 `node_id`）。GitHub 通过 `issue_dependencies_summary.blocked_by` 报告（只算 open blocker，是实时门禁）。dependencies 不可用时退化为子 ticket 正文顶部的 `Blocked by: #<n>, #<n>` 行。所有 blocker 关闭即解除阻塞。
- **前沿查询（frontier）**：列出地图的 open children（`gh issue list --state open`，按地图的 sub-issues / task list 范围过滤），去掉有 open blocker（`issue_dependencies_summary.blocked_by > 0`，或 `Blocked by` 行里有 open issue）或已有 assignee 的；按地图顺序取第一个。
- **认领**：`gh issue edit <n> --add-assignee @me`，这是会话的第一次写操作。
- **解决**：`gh issue comment <n> --body "<answer>"`，然后 `gh issue close <n>`，最后把上下文指针（gist + 链接）追加到地图的 Decisions-so-far。

## 本仓库补充

- 仓库目前没有在用 issue：标签只有 GitHub 默认集，历史上 issue 数为 0。首次使用时需要创建自定义标签（`gh label create`）：五个 triage 标签（见 `docs/agents/triage-labels.md`）和 wayfinder 系列（`wayfinder:map`、`wayfinder:research`、`wayfinder:prototype`、`wayfinder:grilling`、`wayfinder:task`）。
- 开发任务一律在独立 git worktree 中进行，通过 push 任务分支 + PR 合并回 `main`（见根 `AGENTS.md`）。「发布到 issue tracker」创建 issue 不受此限制，但任何代码落地仍走 worktree + PR。
