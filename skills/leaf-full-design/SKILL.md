---
name: leaf-full-design
description: Use for ARC InterfaceDesigner work when the current requirement node owns an executable feature chain.
---

# leaf-full-design

Use this skill when you determine the current node owns the smallest executable feature slice required by its scenarios.

## Instructions

1. Treat the current node as owning the smallest executable feature slice required by its scenarios.
2. Design only interfaces needed for the owned chain, which may span UI -> API -> FUNC -> DB when those layers are truly owned by the leaf.
3. Prefer existing entrypoints, boundary files, services, and persistence scaffolds before creating new owners.
4. Reuse existing parent/dependency interfaces when the leaf should implement, extend, or call them; return the existing `interface_id` so the current node can be attached to the same contract. A reuse recorded only in the `summary` prose attaches the node to nothing.
5. If a parent-designed UI shell is the user-facing entry for the leaf, include that UI interface in the leaf's interface list and connect it to leaf-owned API/FUNC/DB interfaces through callers/callees.
6. Materialize only minimal compilable owner files or skeleton code during DESIGN when the interface would otherwise be only abstract JSON.
7. Do not implement full validation, persistence, authentication/session semantics, encryption, or business behavior in DESIGN; that belongs to TestDrivenDeveloper.
8. Avoid large file writes in DESIGN. Prefer route/client/service/database placeholders with explicit TODO-free contract shape over complete feature implementation.
9. Do not hardcode screenshot data, sample rows, fake success messages, or fallback arrays as completed behavior.
10. Return interfaces that make later test generation straightforward: include `file_path`, `first_line`, responsibility, specification, callers/callees, and test focus.
11. The interface `type` must be one of `UI`, `API`, `FUNC`, or `DB`.
12. Keep the chain connected to existing routes, handlers, tests, and persistence so the leaf compiles into the current system rather than a detached fragment.
13. If the feature affects auth/session/authenticated state/current user/account state, use the auth-session-consistency skill and include global session state, session API, service, and persistence interfaces as needed.
14. If the feature affects cart, checkout, account, products, orders, catalog, inventory, or persisted user-owned data, prefer a connected UI -> API -> FUNC -> DB interface chain over frontend-local state. Omit a layer only when it is clearly not needed by the requirement or existing app architecture.
15. If the requirement description or GIVEN steps say that records, users, relationships, permissions, statuses, or catalog entries already exist, include or reuse a DB interface for deterministic idempotent seed/bootstrap initialization and specify the required ownership, visibility, relationships, and startup/reset timing. Do not treat this as a hidden test fixture or implement it as frontend constants.
16. Explore in ownership order: reused parent/dependency interface, current UI entrypoint, its direct component or hook, current API client, then only the route/service/repository/database scaffold required by the interface chain. Do not inventory unrelated application areas or manifests.
17. The structured response's `interfaces` array must be non-empty for a leaf node: list owned interfaces plus every reused parent/dependency interface with its original `interface_id`, exactly as registered. An empty `interfaces` array fails the DESIGN phase even when `summary` describes the reuse; there is no legitimate zero-interface leaf.
18. Carry requirement-stated UI anchors into the interface contract verbatim: every accessible name, label, route, or message the requirement states in quotes, backticks, or as a distinctly capitalized UI name must appear in the UI interface's `specification` or `test_focus` exactly as stated, so tests and implementation align on the literal instead of a translation.
19. Every control's accessible name is exactly ONE requirement-quoted literal (prefer the primary, first-quoted one): anchored or exact matchers such as `getByLabel(/^密码$|^password$/i)` fail on concatenated text like `密码 Password`, so never merge several quoted literals into one visible string and never translate a quoted literal. For a shared shell control referenced by more than one requirement, ship a literal that every referencing requirement explicitly quotes (the intersection of the quoted-literal sets they state); for example, if both referencing requirements quote `Sign out`, ship `Sign out`. A requirement that lists several acceptable literals (for example `退出登录 或 Sign out`) is satisfied by any one of them alone. Concatenate visible literals only when no single quoted literal satisfies every referencing requirement. Form controls need visible native `<label htmlFor>` associations; `aria-label` may supplement but never replace them, and placeholder text is never the accessible name.
20. When the node's visual reference shows the page divided into labeled sections or steps (for example a registration form split into titled groups), treat those section titles as structural anchors: render each as visible heading text, list them in the UI interface's `specification`, and keep each referenced control inside its referenced section.
