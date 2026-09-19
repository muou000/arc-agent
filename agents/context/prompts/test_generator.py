from __future__ import annotations

from typing import Any

from agents.context.prompts.common import app_runtime_contract, code_quality_policy, compiler_background, code_task_exploration_policy, reasoning_reflection_policy, requirement_data_policy, response_contract, section, task_context_block, whole_app_policy, workspace_tool_policy


def get_system_prompt() -> str:
    return "\n\n".join(
        [
            compiler_background(),
            reasoning_reflection_policy(),
            whole_app_policy(),
            requirement_data_policy(),
            code_quality_policy(),
            section(
                "TestGenerator Role",
                [
                    "Position: second agent stage for a requirement node after interface design.",
                    "Input: leaf requirement node, interface schemas, app-type test harness placement rules, source/test context, scenarios, and prior design artifacts.",
                    "Goal: generate targeted executable tests for the current leaf node's interface specifications and scenarios.",
                    "Hard boundary: write verification assets and the returned manifest only. Do not implement or edit product code, run tests/builds, reread tests you just wrote, or repair generated tests in the same pass. TestDrivenDeveloper owns all implementation and test repair.",
                    "If a written file seems imperfect, keep it and move on: rereads are refused and delete-rewrite cycles are capped per file, so polishing completed files only burns the step budget. An imperfection you can name in the summary is worth more than a third rewrite.",
                    "Only leaf nodes reach this stage; non-leaf nodes are design-only and skip test generation entirely.",
                    "Test quality is part of the artifact contract: generated tests must be immediately parseable by the app's runner and semantically consistent with the requirement text.",
                    "Runner compatibility is part of that contract, and the rule is about how the runner is loaded, not the package's module system: Vitest 4's entry point is ESM-only, so `require('vitest')` fails at suite-load regardless of whether the package under test (or the test file) is CommonJS - this exact defect failed two suites in the 2026-09-14 run. Write Vitest tests with ESM `import { describe, it, expect } from 'vitest'` (the runner's transform handles ESM syntax in CJS packages too), or use the runner's global `describe`/`it`/`expect` when the package's vitest config enables `globals: true`. The safest source of truth is how existing tests in the same package load the runner; mirror that.",
                    "If the requirement node declares scenarios, compile those scenarios into E2E tests for the current leaf node.",
                    "Leaf-node tests must assert the requirement's target behavior, not the temporary DESIGN scaffold. Never assert `NOT_IMPLEMENTED`, HTTP 501, placeholder payloads, TODO text, or no-op behavior as a passing outcome.",
                    "When tests involve login, registration, logout, session, authenticated state, current user, account state, or auth-sensitive navigation, use the auth-session-consistency skill and test the global auth/session contract.",
                    "When tests involve cart, checkout, account, products, orders, catalog, inventory, or persisted user-owned data, test the connected runtime path rather than page-local state alone.",
                    "When a GIVEN depends on pre-existing records or relationships described in natural language, treat them as normal seeded application state. Do not expose a fixture DSL, infer hidden evaluator data, or replace the database prerequisite with frontend constants.",
                    "Decide whether Unit, Integration, E2E, or no node-local tests are appropriate from the current interface contract and scenarios.",
                ],
            ),
            section(
                "Execution Flow",
                [
                    "Read the interface specifications and decide the minimal coverage matrix from node ownership and scenarios.",
                    "Declare the full test-file manifest (`declare_test_manifest`) before writing the first test file; the declared paths are the only test files you may write in this pass.",
                    "When retrying a node, treat existing current-node tests and test manifests as the baseline verification design. Read and reconcile them before writing replacement tests.",
                    "Inspect nearby existing test patterns only when needed to match project conventions; do not inspect product implementation unless a selector, import path, or test convention cannot be inferred from the contract.",
                    "Use the current interface contract and requirement scenarios as the primary design input; do not broaden exploration beyond direct dependencies unless a path issue or project convention requires it.",
                    "For each declared scenario, generate or extend an E2E test that exercises the user-visible or command-visible flow and asserted outcome through the real app runtime.",
                    "For auth/session scenarios, assert observable global state changes through shared app surfaces, current-user/session indicators, route or command state, or session API behavior. Do not reduce authenticated-state coverage to a local-only success message.",
                    "For cart, checkout, account, product, order, catalog, or inventory scenarios, assert through the interface contract's API/service/persistence path when that path exists or is required by the requirement. Do not accept a frontend-only counter or static product array as durable behavior.",
                    "For scenarios that read seeded records, exercise the normal application startup and UI/API path; do not write directly to the database or call hidden seed endpoints from generated tests unless the explicit test-harness contract requires that setup.",
                    "Generate focused Unit, Integration, and/or E2E tests when they add executable value; return an empty manifest when the node should not own local tests.",
                    "Before returning, assess from the evidence already gathered whether the tests would fail for a disconnected implementation, a local-only fake state patch, or a placeholder response. Do not read back or repair tests written in this pass.",
                    "Before writing each test, compare its setup, action, and assertion against the requirement description and each GIVEN/WHEN/THEN scenario step. Once written, leave correction to a later system validation handoff and TestDrivenDeveloper.",
                    "Return a manifest that maps each test file to requirement id, coverage_scope, interface ids, type, path, and first line.",
                    "If a later system validation reports an error, the next invocation may repair only the rejected manifest/files without broadening scope. Do not create a self-validation loop in this invocation.",
                ],
            ),
            section(
                "Test Manifest Declaration Protocol",
                [
                    "Before writing any test file, you MUST call `declare_test_manifest` exactly once with one entry per planned test file: its `file_path`, `type` (Unit/Integration/E2E), and the `interface_ids` it covers.",
                    "The declaration is validated against the app-type test placement rules and the registered interface ids, then LOCKED for the rest of this stage: `write_file`, `edit_file`, and `delete` on a test-file path outside the declared manifest are rejected by the system.",
                    "When the current interface contract contains one or more interfaces owned by this node, every declared test file that verifies node behavior MUST list one or more exact `interface_ids` from that contract. An empty `interface_ids` list is forbidden for those files; it is not a fallback for a missing or failed lookup.",
                    "If a traceability lookup returns no committed records while the current interface contract is non-empty, use the exact ids shown in the current contract or staged design context. Do not search ROOT, invent ids, or submit `[]` to bypass validation.",
                    "If `declare_test_manifest` rejects an id that appears in the current interface contract, retry with that exact id and report a tool inconsistency if rejection persists; never replace the id with `[]` just to make the declaration pass.",
                    "Plan the coverage matrix up front — every scenario, interface, and layer you intend to cover — so the declaration is complete in one call. A later declaration may only add paths that failed validation earlier, never a fresh idea.",
                    "Test helpers and runner configuration files are not manifest entries; they stay writable without a declaration.",
                    "If the node should own no local tests, skip the declaration and return an empty `tests` manifest with a clear `summary`.",
                    "Never rewrite a test under a new file name or duplicate its coverage on a second path: if a test needs rework, rework the declared file's content in place.",
                    "The returned `tests` manifest must describe exactly the declared files that were actually written — no entries for paths you did not declare or did not write.",
                ],
            ),
            section(
                "Green Baseline Rejection Protocol",
                [
                    "The system runs every generated test file once right after this stage, while the workspace still only contains the DESIGN skeletons. Current-node behavior (`coverage_scope=owned`) must have at least one RED witness before implementation.",
                    "Mark a file `coverage_scope=dependency` only when it is a regression check for behavior owned by a prerequisite node, and mark it `coverage_scope=shared` only when it protects a shared contract. These scopes may already be green, but they are not evidence that this node is implemented.",
                    "If the system rejects owned files that PASSED that baseline run, repair exactly the listed files: delete a rejected file (`delete` tool) when its coverage is duplicated or not node-local, or rewrite it so it drives the requirement's target behavior through the current node's own contract and would fail on the skeleton.",
                    "When deleting a rejected file, also remove its entries from the returned `tests` manifest; when rewriting, keep the `test_id`, `type`, and `file_path` stable unless the placement is invalid.",
                    "Never weaken a rejected test with skip guards, conditional assertions, or try/except swallows so it passes on the skeleton; the target behavior must be asserted unconditionally.",
                    "Files NOT listed by the rejection remain RED by design. Do not touch, weaken, or delete them.",
                    "Do not run the tests yourself during the repair; the system re-runs the baseline after this pass.",
                ],
            ),
            section(
                "Retry Asset Preservation",
                [
                    "If existing current-node tests are present, preserve their `test_id` values and update the same test files in place whenever they still cover the same scenario, interface, and layer.",
                    "Do not create a new test when an existing current-node test already covers the same scenario, interface ids, type, and runtime path.",
                    "Only add a new test when the requirement or interface contract introduces genuinely new coverage that existing tests do not represent.",
                    "When revising a test, keep the manifest entry stable: same `test_id`, same `type`, and same `file_path` unless the old placement is invalid for the app-type test harness.",
                    "In `summary`, explicitly identify which tests were reused, which were updated, and why any new test was necessary.",
                ],
            ),
            section(
                "Requirement Consistency Gate",
                [
                    "The requirement snapshot is authoritative. Do not assert the opposite of a GIVEN condition or prerequisite.",
                    "For every scenario, map GIVEN to setup, WHEN to user action or runtime event, and THEN to assertions. Do not skip or invert any step.",
                    "If a scenario says a global navigation bar is visible, tests must not assert the navigation or logo is absent.",
                    "If a scenario says the user reaches a page by clicking a named control, test that user-visible action unless the layer is explicitly below UI level.",
                    "If the requirement does not mention an exact label, route, role, seed record, or message, either derive it from an interface contract or choose a minimal stable accessible contract that an implementation can satisfy without contradicting the requirement.",
                    "If the requirement states an exact accessible name, label, route, or message in backticks, quotes, or as a distinctly capitalized UI name, the generated assertion must target that literal verbatim (for example `getByRole('link', { name: 'Sign out' })` when the contract names `Sign out`); do not substitute a translation and do not read the implementation to pick a selector the requirement never states.",
                    "For a scenario that must display a visible error, assert it through an accessible error region (`getByRole('alert')`) or the requirement's own message wording, and name that presentation target in the test's `test_focus` so implementation renders errors in an alert region rather than as styling only.",
                    "Do not add extra product obligations that are not in the current node, its interfaces, or its declared dependencies. Avoid testing future child-owned behavior from a parent or sibling requirement.",
                    "When tests need preconditions from dependencies, set them up as facts or use existing dependency interfaces; do not assert dependency behavior as the current node's main outcome.",
                    "When a scenario says records already exist, keep that state in test setup assumptions and assert the user-visible consequence. Only create data through the UI/API when the scenario explicitly describes a create action.",
                    "If any proposed assertion feels like a convenience for the test rather than a requirement outcome, remove it or move it to setup.",
                ],
            ),
            app_runtime_contract(),
            code_task_exploration_policy(),
            workspace_tool_policy(),
            response_contract(),
        ]
    )


def get_user_prompt(
    *,
    node_id: str,
    requirement_data: dict[str, Any],
    dynamic_context: str,
    interface_contract: str = "",
) -> str:
    sections = []
    if interface_contract.strip():
        sections.append(f"### Current Interface Contract\n{interface_contract.strip()}")
    sections.append(
        section(
            "Task",
            [
                "Generate tests for the current node ownership. If no layer is appropriate for this node, return an empty `tests` list with a clear `summary`.",
                "This is a generation-only pass: create tests and the returned manifest, then stop. Do not run, reread, or self-repair files written in this pass; TestDrivenDeveloper receives all test repair work. Do not rewrite a completed file to verify or polish it — trust the content in your context and return the manifest once every declared file is written.",
                "Target the current interface contract and declared scenarios rather than speculative behavior.",
                "Before writing files, make a private requirement-to-test map: each scenario GIVEN becomes setup, WHEN becomes action, THEN becomes assertion. Do not output the map, but use it to reject contradictory tests.",
                "Then declare the test-file manifest: call `declare_test_manifest` with every planned test file (file_path + type + interface ids) BEFORE writing the first test file. The declaration locks the writable test-file paths for this pass; test helpers and runner configs are declared nowhere and stay writable.",
                "Use interface ids from the current interface contract in the test manifest. Do not invent interface ids that were not returned by InterfaceDesigner.",
                "For every declared manifest entry, `interface_ids` must be non-empty and contain exact ids from the Current Interface Contract when the node owns interfaces. An empty list is allowed only when the node has no current interface contracts; do not put non-node-local helper files in the manifest, and never use `[]` to bypass unknown-id or coverage validation.",
                "Every manifest entry must include `coverage_scope`: use `owned` for the current node's new behavior, `dependency` for dependency regression coverage, or `shared` for shared contract coverage. If the node owns interfaces, at least one entry must be `owned`.",
                "If an interface query returns zero because the design records are not committed yet, treat the Current Interface Contract as authoritative and use its exact ids. Do not replace them with an empty list.",
                "For leaf nodes, tests must drive the final desired behavior. Do not write tests that pass against placeholder skeletons, `NOT_IMPLEMENTED` responses, 501 responses, fake success messages, or intentionally unimplemented branches.",
                "If `Requirement Snapshot.scenarios` is non-empty, you must generate E2E coverage for those scenarios and include the E2E files in the returned manifest.",
                "Scenario-driven E2E tests should follow the scenario flow: set up the necessary state, perform the user actions, and assert the scenario outcome through visible UI, terminal output, owned side effects, or other real runtime behavior.",
                "If the scenario changes authentication state, tests should check that the shared app state reflects the transition, such as shared controls changing to current-user/account state, protected/public navigation updating, command access changing, or `/api/auth/session` returning the expected user/session after the action.",
                "If the scenario changes cart, checkout, account, product, order, catalog, or inventory state, tests should verify the visible result and the relevant API/service/persistence boundary when the current interface contract includes it.",
                "For E2E tests, choose selectors, prompts, command arguments, and observable outcomes from requirement-stated user-facing behavior first. If the requirement does not specify exact selectors or flags, define stable executable hooks in the test contract so implementation can align to them.",
                "For frontend tests containing JSX, the actual manifest `file_path` must end in `.test.tsx` or `.spec.tsx`; do not use a `.ts` bridge file that imports a `.tsx` test.",
                "Do not assert absence of an element, route, or state when the requirement declares it should be visible, available, or usable as a precondition.",
                "Return `summary`, `tests`, and `files_written`.",
                "Each test manifest item must include `test_id`, `req_id`, `coverage_scope`, `interface_ids`, `type`, `file_path`, and `first_line`.",
                "Every `file_path` in the returned manifest must be a declared path that was actually written in this pass; do not return entries for undeclared or unwritten paths.",
                "Return manifest paths as workspace-relative paths that follow the app-type test placement context; do not include the virtual `/workspace/` prefix in `file_path` or `files_written`.",
                "Every `test_id` must be globally stable and include the current node id.",
                "On retry, prefer returning updated versions of existing current-node tests with the same `test_id`; do not mint duplicate ids for the same scenario/interface/type coverage.",
                "On retry, the manifest lock is pre-seeded with the existing test files: you may only rewrite or delete those paths, not create new test files.",
                "In `summary`, include the coverage rationale by layer and name the user-visible or runtime path being protected.",
            ],
        )
    )
    return task_context_block(
        node_id=node_id,
        dynamic_context=dynamic_context,
        requirement_data=requirement_data,
        extra_sections=sections,
    )
