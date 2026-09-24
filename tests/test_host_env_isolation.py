"""Scheduling switches must never leak into unit tests from the host (issue #146).

``core.workflow`` runs ``load_project_env()`` at import time, copying the
repository ``.env`` into ``os.environ`` (without overriding existing
variables). When a host ``.env`` or shell sets one of the scheduling switches,
tests that assert default scheduling semantics
(``test_parallel_scheduling_rules``, ``test_parallel_worktree_drain``) go red
in a batch even though the code is fine. The autouse
``isolate_scheduling_switches`` fixture in ``tests/conftest.py`` deletes the
switches before every test; tests that exercise a switch explicitly
``monkeypatch.setenv`` over it (the fixture runs first, so the explicit value
wins).

The guards below must also work on a clean host (issue #153): the pytester
tests inject the pollution themselves and run pytest in-process against the
suite's real ``tests/conftest.py``, so the autouse fixtures prove effective
regardless of what the host happens to set - fixtures in place, the inner run
is green; plugin (and thus fixtures) missing, the same inner run goes red.
The registry guards keep the switch names themselves honest: a single
authoritative table in ``core/scheduling_switches.py`` feeds both the core
read points and the scrub list, and any unregistered ``ARC_*`` literal in
``core`` fails here instead of silently bypassing isolation.
"""

from __future__ import annotations

import ast
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from agents.runtime import rebase_gate
from core import merge_arbitration, workflow
from core.scheduling import design_pipelining_enabled
from core.scheduling_switches import SCHEDULING_SWITCH_ENV_VARS
from tests.conftest import MODEL_ENV_VARS_TO_CLEAR

pytest_plugins = "pytester"

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", SCHEDULING_SWITCH_ENV_VARS)
def test_scheduling_switch_never_inherits_a_host_value(name: str) -> None:
    """The autouse scrub must have removed the switch before the test body."""

    value = os.environ.get(name)
    assert not value, (
        f"{name}={value!r} leaked from the host environment into the test "
        "process; scheduling-semantics tests assert default behaviour and "
        "would false-red in a batch (issue #146). Check "
        "isolate_scheduling_switches in tests/conftest.py."
    )


def test_scheduling_helpers_fall_back_to_defaults() -> None:
    """With no switch in the environment the helpers yield default semantics."""

    assert workflow._worktrees_enabled() is False
    assert workflow._affinity_depth() == 1
    assert design_pipelining_enabled() is False
    assert workflow.ARCWorkflowManager._auto_tdd_retry_enabled() is True
    assert merge_arbitration.arbitration_enabled() is False
    assert rebase_gate.rebase_on_merge_enabled() is False


def test_explicit_test_override_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests that pin a switch explicitly keep working over the scrub."""

    monkeypatch.setenv("ARC_AFFINITY_DEPTH", "2")
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    assert workflow._affinity_depth() == 2
    assert design_pipelining_enabled() is True


# --- Mechanical teeth for the isolation fixtures (issue #153) ---------------
#
# The parametrized host-value assertions above are trivially true on a clean
# host (CI has no leaks), so deleting the autouse fixtures would not turn CI
# red. The pair below closes that hole: pollution is injected here regardless
# of the host's actual state, the inner run loads the suite's real conftest
# as a plugin (same source every test uses - no test-local copy), and the
# control run proves the inner assertion can fail.


def _run_isolation_guard_inner(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    names: tuple[str, ...],
    *,
    with_conftest_plugin: bool,
) -> pytest.RunResult:
    """Run a tiny inner pytest session against injected host pollution.

    ``-p tests.conftest`` registers ``tests/conftest.py`` (already imported
    by this session) as a plugin of the inner run, so its autouse fixtures
    apply exactly as they would to any test in this suite.
    """

    for name in names:
        monkeypatch.setenv(name, "host-polluted")
    # Keep the host from steering the inner run through pytest's env vars.
    for var in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        monkeypatch.delenv(var, raising=False)
    pytester.makepyfile(
        inner=(
            "import os\n"
            "\n"
            "\n"
            "def test_inner_sees_no_pollution():\n"
            f"    leaked = {{name: os.environ.get(name) for name in {names!r}}}\n"
            "    assert not any(value is not None for value in leaked.values()), leaked\n"
        )
    )
    pytester.syspathinsert(str(REPO_ROOT))
    args = ["-p", "no:anyio", "-p", "no:xdist"]
    if with_conftest_plugin:
        args += ["-p", "tests.conftest"]
    args.append("inner.py")
    return pytester.runpytest(*args)


@pytest.mark.parametrize(
    "names",
    [SCHEDULING_SWITCH_ENV_VARS, MODEL_ENV_VARS_TO_CLEAR],
    ids=["scheduling-switches", "model-env"],
)
def test_isolation_fixtures_scrub_injected_pollution(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    names: tuple[str, ...],
) -> None:
    """With the real conftest loaded, injected pollution comes out scrubbed."""

    result = _run_isolation_guard_inner(pytester, monkeypatch, tuple(names), with_conftest_plugin=True)
    result.assert_outcomes(passed=1)


@pytest.mark.parametrize(
    "names",
    [SCHEDULING_SWITCH_ENV_VARS, MODEL_ENV_VARS_TO_CLEAR],
    ids=["scheduling-switches", "model-env"],
)
def test_isolation_guards_have_teeth(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    names: tuple[str, ...],
) -> None:
    """Control: without the conftest plugin the same inner run must go red.

    This is what makes the green run above load-bearing: deleting or breaking
    the autouse fixtures turns ``test_isolation_fixtures_scrub_injected_pollution``
    red on every host, including the clean environment CI runs in.
    """

    result = _run_isolation_guard_inner(pytester, monkeypatch, tuple(names), with_conftest_plugin=False)
    result.assert_outcomes(failed=1)


# --- Registry contract (issue #153) -----------------------------------------
#
# SCHEDULING_SWITCH_ENV_VARS lives in core/scheduling_switches.py, shared by
# the core read points and the scrub list. The guards below keep that
# registry complete: an unregistered literal means a switch the isolation
# fixtures cannot know about - exactly the ARC_AUTO_TDD_RETRY gap that
# motivated them.

_ARC_ENV_NAME = re.compile(r"ARC_[A-Z0-9_]+\Z")

# Non-scheduling ARC_* variables read in core/. They do not flip scheduling
# semantics, so the isolation fixtures rightly ignore them; register any new
# entry here with a one-line reason. Model-wiring vars are additionally
# scrubbed by ``isolate_model_env`` (see MODEL_ENV_VARS_TO_CLEAR).
_NON_SCHEDULING_CORE_ENV_VARS = frozenset(
    {
        # app-type wiring
        "ARC_ANDROID_PACKAGE",
        "ARC_APP_TYPE",
        "ARC_WEB_PORT",
        # subprocess env whitelist: the explicit generated-app contract keys
        # (core/processes.py; the template's vite/playwright configs and
        # database scaffold read these)
        "ARC_WEB_BASE_URL",
        "ARC_DB_FILE",
        "ARC_E2E_DB_LABEL",
        # debug/logging
        "ARC_DEBUG",
        "ARC_DEBUG_LOG_PATH",
        "ARC_LOG_COLOR",
        "ARC_TIMEZONE",
        # path resolution and git identity
        "ARC_ENV_FILE",
        "ARC_WORKSPACE_ROOT",
        "ARC_GIT_USER_NAME",
        "ARC_GIT_USER_EMAIL",
        # model client wiring
        "ARC_MODEL_CONNECT_TIMEOUT",
        "ARC_MODEL_MAX_CONSECUTIVE_FAILURES",
        "ARC_MODEL_MAX_RETRIES",
        "ARC_MODEL_RETRY_DELAY",
        "ARC_MODEL_RETRY_MAX_DELAY",
        "ARC_MODEL_STREAM_TRANSPORT",
        "ARC_MODEL_STREAM_USAGE",
        "ARC_MODEL_TIMEOUT",
        "ARC_OPENAI_API_MODE",
        # visual analysis
        "ARC_VISUAL_ANALYSIS_CONCURRENCY",
        "ARC_VISUAL_PRECOMPUTE",
        "ARC_VISUAL_PRECOMPUTE_CONCURRENCY",
    }
)


def _python_files(*roots: Path) -> Iterator[Path]:
    for root in roots:
        if root.is_file():
            yield root
        else:
            yield from sorted(root.rglob("*.py"))


def _arc_env_string_constants(tree: ast.AST) -> set[str]:
    """Exact ``ARC_*`` string constants in an AST, docstrings excluded."""

    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and _ARC_ENV_NAME.fullmatch(node.value)
        ):
            names.add(node.value)
    return names


def test_core_env_var_literals_are_registered() -> None:
    """Every ``ARC_*`` constant in core/ must be registered (issue #153).

    Register a scheduling switch in ``core/scheduling_switches.py`` (it then
    joins the scrub list and the pytester teeth guards automatically) or
    exempt a non-scheduling read in ``_NON_SCHEDULING_CORE_ENV_VARS`` with a
    one-line reason.
    """

    unregistered: dict[str, list[str]] = {}
    for path in _python_files(REPO_ROOT / "core"):
        if path.name == "scheduling_switches.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        unknown = _arc_env_string_constants(tree) - set(SCHEDULING_SWITCH_ENV_VARS) - _NON_SCHEDULING_CORE_ENV_VARS
        for name in unknown:
            unregistered.setdefault(name, []).append(str(path.relative_to(REPO_ROOT)))
    assert not unregistered, (
        f"Unregistered ARC_* env literals in core/: {unregistered}. "
        "Register scheduling switches in core/scheduling_switches.py (the "
        "isolation fixtures then cover them) or exempt non-scheduling reads "
        "in _NON_SCHEDULING_CORE_ENV_VARS with a reason."
    )


def test_scheduling_switch_names_stay_out_of_foreign_literals() -> None:
    """Registered scheduling switches are imported, never re-literalized.

    Read points outside core (agents middleware, app-type handlers) must take
    the name from the registry module so a rename cannot strand a literal
    (issue #153); tests may hardcode names freely.
    """

    registered = set(SCHEDULING_SWITCH_ENV_VARS)
    offenders: dict[str, list[str]] = {}
    for path in _python_files(REPO_ROOT / "agents", REPO_ROOT / "app_type_handler"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in _arc_env_string_constants(tree) & registered:
            offenders.setdefault(name, []).append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        f"Scheduling switch names hardcoded outside core/: {offenders}. "
        "Import the constant from core.scheduling_switches instead."
    )


def test_scheduling_switch_registry_stays_a_pure_leaf() -> None:
    """The registry module must stay import-safe before any config loading.

    ``tests/conftest.py`` imports it in every session; an import (even a
    ``from __future__`` line) risks dragging in ``load_project_env()`` - the
    leak the isolation fixtures neutralize. Constants only, and the declared
    tuple must match the module's ``ARC_*`` constants exactly, in both
    directions: a constant without a tuple entry is a switch the scrub list
    silently misses, a tuple entry without a constant is a dangling name.
    """

    path = REPO_ROOT / "core" / "scheduling_switches.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert not imports, "core/scheduling_switches.py must stay free of imports"

    constants: dict[str, str] = {}
    allowed = (ast.Assign, ast.Expr)
    for node in tree.body:
        assert isinstance(node, allowed), (
            f"core/scheduling_switches.py must stay pure constants, found {type(node).__name__}"
        )
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    declared = set(SCHEDULING_SWITCH_ENV_VARS)
    declared_constants = {name for name, value in constants.items() if _ARC_ENV_NAME.fullmatch(value)}
    assert declared == declared_constants, (
        "SCHEDULING_SWITCH_ENV_VARS must register every ARC_* constant in "
        "core/scheduling_switches.py and nothing else: constants without a "
        f"tuple entry leak host pollution silently; missing constants: "
        f"{sorted(declared_constants - declared)}, stale entries: "
        f"{sorted(declared - declared_constants)}"
    )
    for name in SCHEDULING_SWITCH_ENV_VARS:
        assert constants.get(name) == name, (
            f'registry entry must be a plain constant: {name} = "{name}"'
        )
