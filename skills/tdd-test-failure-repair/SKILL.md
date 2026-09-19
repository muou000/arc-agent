---
name: tdd-test-failure-repair
description: Use for ARC TestDrivenDeveloper work after run_tests fails or when repairing implementation against a current failing batch.
---

# tdd-test-failure-repair

Use this skill after `run_tests` reports a failing current batch.

## Instructions

1. Treat the latest `run_tests` output as the source of truth.
2. Classify the failure before editing: implementation logic, boundary wiring, selector/render state, persistence/test database, framework/config, generated test defect, or test content.
3. Re-read the failing test file and the nearest owner implementation file before making another edit.
4. Compare the failure against the current UI/API/FUNC/DB interface chain; if a downstream layer is missing or disconnected, repair the chain rather than patching only the surface assertion.
5. If the same fingerprint repeats, replace the current hypothesis or move one layer outward instead of patching neighboring files from momentum. If ARC_TEST_FILE_STATUS reports STALL DETECTED (same fingerprint 3 times in a row), rotation is mandatory: re-classify the failure, list the hypotheses already tried, and edit a different layer of the UI/API/FUNC/DB chain or a different fix approach within the same layer.
6. If product behavior is wrong, edit product code. If a generated test is invalid, contradictory, brittle, or incompatible with the runner, edit the test while preserving requirement intent. If runner setup is wrong, edit build/test configuration.
7. Make one minimal contract-preserving fix, then call `run_tests` again. Work file-by-file when the layer has several red files: verify each red file green with `run_tests(test_files=[...])`, then close the layer with one passing full-layer run.
8. Do not stop after failing runs while the active layer's tool budget remains. Do not declare blocked, failed, impossible, or out-of-scope as a final answer.
9. Say `IMPLEMENTED` only when the system indicates the active layer passed and no scheduled layer remains blocked by the current session.
10. Each current-node test layer has an independent `run_tests` budget of 10 calls. After a failure, spend enough effort to localize and repair the cause before consuming the next call.
11. For auth/session failures, use the auth-session-consistency skill and repair the shared session path first: session persistence, current-session API, client session loader, global provider/state, shell/header consumers, and route behavior.
12. If Playwright reports an unknown fixture parameter, inspect the `test.extend` block and make the fixture name and `use(...)` value match before rerunning.
13. If Playwright or Testing Library reports multiple matches for a label, do not retry with a broader selector. Use an exact accessible role, a stable id, or a scoped locator that identifies one element.
14. If the same backend database singleton error repeats, inspect the database runtime helper and initialization lifecycle before changing unrelated tests or UI files.
15. When repairing a contradictory generated test, re-check the requirement scenario: GIVEN remains setup, WHEN remains action, THEN remains assertion. Preserve the scenario intent instead of weakening it to match current implementation.
16. For cart, checkout, account, product, order, catalog, inventory, or persisted user-owned data failures, repair the connected domain path first: data source, API route/client, service logic, persistence/runtime state, shared consumer, and visible UI.
17. If the failure is a missing pre-existing record, relationship, permission, status, or list item described by the requirement, inspect and repair the schema/repository/seed/bootstrap/startup path first. Do not patch the UI with hardcoded rows or add test-only database setup.
18. Begin each repair with the failing manifest test, its direct owner file, the current-node interface contract, and the exact failure output. Read configuration, shared runtime, or neighboring layers only when that evidence points there; do not restart project-wide exploration.
19. If the E2E failure body contains `=== Backend Process Output ===`, read that section first: it is the backend server's own console output, and a stack trace there (for example Express 5's bare-wildcard throw `TypeError: Cannot read properties of undefined (reading 'type')` from `app.get('*')` or `app.use('*')`) means the server crashed at startup. Fix the crashing registration (use a regex or `'/{*splat}'` for SPA fallback) instead of editing tests or runtime wiring.

20. When a full-layer run passes, the layer is done: do not keep editing its tests or covered code. Re-editing a green layer risks turning it red again and spending the budget recovering a state you already had. Move to the next layer or return `IMPLEMENTED` per the layer ladder.
21. Never silence a failing test by adversarial means: no zero-width/invisible characters inside label text, no deleting assertions, no widening matchers beyond what the requirement states, no renaming visible UI text so a strict locator stops matching. If `getByText`/`getByLabel` reports multiple matches, fix the semantic structure - distinct accessible names, `role="alert"` for error messages - rather than obfuscating one of the matching elements.
22. When a React test asserts an exact count of effect-driven calls (fetch, subscriptions), check the render environment for StrictMode first: it double-invokes effects by design. Either the component deduplicates intentionally (guard inside the effect) or the assertion accounts for it; do not burn run_tests calls re-asserting the same count after neighboring edits.
