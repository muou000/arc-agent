---
name: test-driven-development
description: Use during the ARC IMPLEMENT stage when TestDrivenDeveloper turns TestGenerator's baseline RED evidence green with run_tests and run_build
---

# ARC IMPLEMENT Test-Driven Repair

This skill is for the ARC `IMPLEMENT` stage only. `TestGenerator` has already
created the registered test files, and the compiler has run them against the
DESIGN skeletons. Treat the `Baseline RED Evidence` in the task context and
the latest `run_tests` result as the source of truth for the repair queue.

## Stage Contract

- Work from the requirement snapshot, interface contract, test manifest, and
  baseline RED evidence supplied by ARC.
- Make the smallest product-code change that satisfies the current failing
  behavior. Preserve the registered tests and their intended assertions.
- Validation is provided by ARC's `run_tests` and `run_build` tools. Do not
  invoke shell commands or substitute a hand-run test command.
- The stage may edit product files, but it must never remove product files or
  template files. A diagnostic test created during this pass may be removed
  only when the stage itself wrote it and the stage rules permit that cleanup.
- Do not modify a green test or the product code it covers after its layer is
  closed. Do not weaken, delete, or rewrite a registered test to avoid a
  failure.
- Return `IMPLEMENTED` only after the latest full-layer `run_tests` result has
  `Exit Code: 0`.

## Repair Loop

1. Read the named failing test and the nearest owner file before broadening
   exploration. Classify the failure as product behavior, test setup,
   configuration, dependency, or runner/environment failure.
2. If this is the first pass and no failure handoff requires immediate repair,
   complete the initial implementation pass before calling `run_tests`.
3. Prefer one focused change for one failing test file. Use
   `run_tests(test_files=[...])` to verify that file when the active layer
   allows it, then continue through the remaining red files.
4. Use `run_tests()` or `run_tests(test_type=...)` without `test_files` for
   the final full-layer check. A passing subset does not close the layer.
5. When a run reports an environment failure, make at most one concrete repair
   and validate it with `run_tests` again. Use `install_dependencies` only
   when the failure identifies a missing package and the stage exposes that
   tool. Use `run_build` for build failures or after a change whose contract
   requires a build check.
6. If the output reports `STALL DETECTED`, rotate the hypothesis and change a
   different layer of the UI/API/FUNC/DB chain or use a materially different
   repair. If it reports `ARC_TDD_HARD_STOP` or `Deterministic TDD Stop`, stop
   spending test calls and return the evidence for the compiler.

## Test Discipline

- Assert observable behavior at the contract boundary, not implementation
  trivia or a mock's existence.
- Keep expected values independently derived from the requirement. Do not make
  an assertion by calling the same helper that produced the actual value.
- Keep setup, action, and assertion scoped to one behavior. Use the existing
  test harness and manifest paths instead of adding unrelated fixtures.
- Preserve complete mock shapes when a slow or external dependency must be
  isolated. Prefer real components at the boundary under repair.
- For React tests, account for StrictMode effect double invocation before
  changing an exact-call-count assertion. Never silence a failure with hidden
  characters, removed assertions, or weakened selectors.

## Completion Check

Before ending the pass, confirm that:

- every current-layer registered test is green;
- the full-layer `run_tests` call passed after the last product change;
- any build check required by the failure output passed;
- no diagnostic file or unrelated artifact remains in the workspace; and
- the final response is concise and uses `IMPLEMENTED` only when the ARC
  result proves completion.

When a layer is closed because its budget is exhausted, do not claim success.
Report the latest failure fingerprint, the edits attempted, and the next
concrete owner file for the compiler's continuation handoff.
