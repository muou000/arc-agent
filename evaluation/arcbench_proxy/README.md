# ARC-Bench Proxy Acceptance

This directory contains independent black-box Playwright tests for the two local
hackathon tasks. The tests are not generated TDD tests and are not the official
hidden suite. They expose end-to-end regressions before using the official local
simulation runner.

The official runner lives outside this repository at:

    D:\code\hackathon-local-simulation

It accepts explicit requirements and test directories, so these proxy suites can
run without modifying the runner or copying challenge data into this repository.

## Run one task

PowerShell:

    python D:\code\hackathon-local-simulation\local_submit.py run `
      --agent path\to\arc-agent.zip `
      --competition hackathon `
      --task hackathon-sheet `
      --requirements-dir D:\code\arc-agent\arc-bench-test\hackathon-sheet\requirements `
      --tests-dir D:\code\arc-score-design-20260926\evaluation\arcbench_proxy\tests\hackathon-sheet `
      --output-dir path\to\runs\sheet-001 `
      --env-file path\to\.env

The repository-level wrapper runs both tasks and aggregates their test counts and
model cost. It does not claim to reproduce the official hidden test count.

## Run both tasks

PowerShell:

    python evaluation\arcbench_proxy\run_combined.py `
      --agent path\to\arc-agent.zip `
      --simulation-root D:\code\hackathon-local-simulation `
      --sheet-requirements D:\code\arc-agent\arc-bench-test\hackathon-sheet\requirements `
      --github-requirements D:\code\arc-agent\arc-bench-test\hackathon-github\requirements `
      --output-root path\to\runs\combined-001 `
      --env-file path\to\.env `
      --show-tests

The wrapper uses the screenshot formula with a default reasonable cost of 0.4 per
passed test. Use `--cost-per-pass` when the official scoring configuration changes.

## Test boundary

- Repository Contract Test: tests arc-agent itself.
- Generated TDD Test: tests written by the compiler for a generated app.
- Independent Proxy Acceptance: the tests in this directory.
- Official Hidden Test: the platform-only test suite.

Proxy results are evidence for iteration, not proof of the official score.

## Inspect coverage

The coverage report reads the official requirements tree and the covers comments in
the proxy tests. Run it with the requirements directory and matching test directory:

    python evaluation\\arcbench_proxy\\coverage_report.py --requirements-dir D:\\code\\arc-agent\\arc-bench-test\\hackathon-sheet\\requirements --tests-dir evaluation\\arcbench_proxy\\tests\\hackathon-sheet

The initial suite is intentionally small. Expand it by adding a black-box test and
a covers comment, then keep a separate frozen subset for regression comparison.
