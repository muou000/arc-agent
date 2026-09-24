"""Authoritative registry of scheduling-switch environment variable names.

Pure-constant leaf module: no imports, no side effects. ``tests/conftest.py``
takes the isolation-fixture scrub list from here, so importing this file must
never drag in configuration loading - importing any other ``core`` module
(e.g. ``core.workflow``) runs ``load_project_env()`` at import time, which is
the very host leak the isolation fixtures neutralize (issue #153).

Every scheduling-switch read point takes its variable name from this table
instead of an inline literal, and a new switch must be registered here before
its first read; ``tests/test_host_env_isolation.py`` enforces both directions
mechanically (an unregistered ``ARC_*`` constant in ``core`` fails the suite,
and a registered switch's name may not reappear as a literal outside ``core``).
"""

ARC_NODE_WORKTREES = "ARC_NODE_WORKTREES"
ARC_MAX_CONCURRENT_TASKS = "ARC_MAX_CONCURRENT_TASKS"
ARC_AFFINITY_DEPTH = "ARC_AFFINITY_DEPTH"
ARC_DESIGN_GATE_PIPELINE = "ARC_DESIGN_GATE_PIPELINE"
ARC_MERGE_ARBITRATION = "ARC_MERGE_ARBITRATION"
ARC_REBASE_ON_MERGE = "ARC_REBASE_ON_MERGE"
ARC_AUTO_TDD_RETRY = "ARC_AUTO_TDD_RETRY"
ARC_TDD_RETRY_FRESH_THREAD = "ARC_TDD_RETRY_FRESH_THREAD"

# Scheduling/merge semantics switches (see core/workflow.py). They reach
# ``os.environ`` through core.workflow's import-time ``load_project_env()``
# when the host shell or ``.env`` sets them, but they flip scheduling
# semantics instead of provider wiring, so test isolation scrubs them
# explicitly; see ``tests/conftest.py``.
SCHEDULING_SWITCH_ENV_VARS = (
    ARC_NODE_WORKTREES,
    ARC_MAX_CONCURRENT_TASKS,
    ARC_AFFINITY_DEPTH,
    ARC_DESIGN_GATE_PIPELINE,
    ARC_MERGE_ARBITRATION,
    ARC_REBASE_ON_MERGE,
    ARC_AUTO_TDD_RETRY,
    ARC_TDD_RETRY_FRESH_THREAD,
)
