"""Faux-model compile e2e over the keep-req2 fixture (#80).

The deliverable of the fixture ticket: the ``compile`` pipeline runs the real
scheduler end to end on ``arc-bench-test/keep-req2`` — parallel worktree mode
with ``ARC_AFFINITY_DEPTH=2`` — with a scripted faux model and a scripted app
handler. No real model calls, no npm. The real deep-agents loops, stage
discipline, manifest lock, DESIGN baseline gate, TDD layer scheduling, git
worktrees/merges, and the traceability store all execute.

Mechanics: parallel drain builds one phase runner (and stage adapters) per
task, and tasks of different nodes interleave, so a single FIFO script cannot
work. ``_NodeRoutedFauxModel`` instead keeps one script per node and routes by
the current node id, which every stage prompt leads with (``### Current Node``
in ``agents/context/prompts/common.py``); the per-task runners get the model
and the scripted app handler injected by patching
``ARCWorkflowManager._build_task_phase_runner`` — the same seam the manager
itself uses to build them.

What one leaf node's script covers (mirrors the genuine agent behaviour the
existing per-adapter faux tests script):

- DESIGN: skeleton write + ``InterfaceDesignResponse``, then
  ``declare_test_manifest`` + test-file write + ``TestGenerationResponse``;
  the scripted handler answers the baseline run RED;
- IMPLEMENT: implementation write + ``run_tests`` (PASS) + ``IMPLEMENTED``;
  the baseline already seeded the RED state, so one passing run closes the
  layer (the "all green, system-run regression" fast path).

Non-leaf nodes consume no model turns at all: without visual references their
DESIGN takes the skip path and their IMPLEMENT completes directly after
interface materialization — the fixture's non-leaf nodes carry no
``visual_reference``, which is exactly the parent tree's shape.

The assertions are on the observable contract the later levers (#82/#83) will
run against: the queue drains fully, every node reaches its terminal state,
``.arc/processing_queue.json`` and the traceability tables land on disk, and
the affinity map keeps the six feature subtrees in six groups.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr

from core import config as core_config
from core.files import load_requirements
from core.service import configure_runtime, reset_runtime_for_tests
from core.workflow import ARCWorkflowManager, NODE_CONVERGED, NODE_PASSED
from tests.helpers.faux import (
    FakeAppHandler,
    FauxChatModel,
    failing_test_output,
    passing_test_output,
    faux_text,
    faux_tool_call,
)
from app_type_handler.test_results import parse_test_run
from tests.test_workflow.test_keep_req2_fixture import (
    FEATURE_SUBTREES,
    KEEP_REQ2_YAML,
)


# ---------------------------------------------------------------------------
# Per-node scripts
# ---------------------------------------------------------------------------


def _walk_nodes(tree: dict) -> list[tuple[dict, bool]]:
    """(node, is_non_leaf) for every node including the root, in pre-order.

    The root is included (it runs DESIGN/IMPLEMENT tasks like any folder
    node) but is not part of the per-node scripts: the drain's task for it
    follows the same non-leaf paths as the other folder nodes.
    """
    out: list[tuple[dict, bool]] = []

    def walk(node: dict) -> None:
        children = [c for c in node.get("children") or [] if isinstance(c, dict)]
        out.append((node, bool(children)))
        for child in children:
            walk(child)

    walk(tree)
    return out


def _design_turns(node: dict) -> list[BaseMessage]:
    node_id = node["id"]
    slug = node_id.lower().replace("-", "_")
    interface_id = f"{node_id}-FUNC-{slug}"
    return [
        faux_tool_call(
            "write_file",
            {
                "file_path": f"/workspace/backend/src/features/{slug}.js",
                "content": f"// skeleton for {node_id}\nexport const {slug} = {{}};\n",
            },
            call_id=f"{node_id}-design-write",
        ),
        faux_tool_call(
            "InterfaceDesignResponse",
            {
                "summary": f"Shell contracts for {node_id}.",
                "interfaces": [
                    {
                        "interface_id": interface_id,
                        "type": "FUNC",
                        "name": slug,
                        "responsibility": f"Owns the {node['name']} feature surface.",
                        "file_path": f"backend/src/features/{slug}.js",
                        "first_line": f"export const {slug}",
                        "callers": [],
                        "callees": [],
                    }
                ],
                "files_written": [f"backend/src/features/{slug}.js"],
            },
            call_id=f"{node_id}-design-response",
        ),
    ]


def _testgen_turns(node: dict) -> list[BaseMessage]:
    node_id = node["id"]
    slug = node_id.lower().replace("-", "_")
    test_path = f"backend/tests/unit/{slug}.test.js"
    interface_id = f"{node_id}-FUNC-{slug}"
    return [
        faux_tool_call(
            "declare_test_manifest",
            {
                "files": [
                    {
                        "file_path": test_path,
                        "type": "Unit",
                        "interface_ids": [interface_id],
                    }
                ]
            },
            call_id=f"{node_id}-declare",
        ),
        faux_tool_call(
            "write_file",
            {
                "file_path": f"/workspace/{test_path}",
                "content": f"test('{node_id}', () => {{ expect(true).toBe(true); }});\n",
            },
            call_id=f"{node_id}-test-write",
        ),
        faux_tool_call(
            "TestGenerationResponse",
            {
                "summary": f"One unit test for {node_id}.",
                "tests": [
                    {
                        "test_id": f"{node_id}-T1",
                        "req_id": node_id,
                        "interface_ids": [interface_id],
                        "type": "Unit",
                        "coverage_scope": "owned",
                        "file_path": test_path,
                        "first_line": f"test('{node_id}'",
                    }
                ],
                "files_written": [test_path],
            },
            call_id=f"{node_id}-testgen-response",
        ),
    ]


def _implement_turns(node: dict) -> list[BaseMessage]:
    node_id = node["id"]
    slug = node_id.lower().replace("-", "_")
    return [
        faux_tool_call(
            "write_file",
            {
                "file_path": f"/workspace/backend/src/features/{slug}.js",
                "content": f"// implementation for {node_id}\nexport const {slug} = {{ done: true }};\n",
            },
            call_id=f"{node_id}-impl-write",
        ),
        faux_tool_call("run_tests", {"test_type": "Unit"}, call_id=f"{node_id}-impl-run"),
        faux_text("IMPLEMENTED"),
    ]


def _node_script(node: dict, is_non_leaf: bool) -> list[BaseMessage]:
    """The scripted turns one node consumes across the whole drain.

    A non-leaf node without visual references skips DESIGN entirely (the
    phase's skip path) and its IMPLEMENT completes directly after interface
    materialization, so it consumes zero model turns; only leaves run the
    design -> testgen -> implement cycle.
    """
    if is_non_leaf:
        return []
    return [*(_design_turns(node)), *(_testgen_turns(node)), *(_implement_turns(node))]


class _NodeRoutedFauxModel(FauxChatModel):
    """Faux model with one script per node, routed by the prompt's node id.

    The parallel drain interleaves tasks of different nodes, so a shared FIFO
    queue would hand one node's scripted turns to another. Every stage user
    prompt starts with ``### Current Node\\n`<id>````, so the first human
    message identifies the addressee; the model then pops only that node's
    turns.
    """

    responses: list[BaseMessage] = Field(default_factory=list)
    _scripts: dict = PrivateAttr(default_factory=dict)
    _routed: dict = PrivateAttr(default_factory=dict)
    _first_call_checked: bool = PrivateAttr(default=False)

    def set_node_scripts(self, scripts: dict[str, list[BaseMessage]]) -> None:
        self._scripts = {node_id: list(turns) for node_id, turns in scripts.items()}

    def routed_nodes(self) -> set[str]:
        """Node ids this model has served at least one scripted turn for."""
        return set(self._routed)

    def pending_counts(self) -> dict[str, int]:
        return {node_id: len(turns) for node_id, turns in self._scripts.items() if turns}

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._calls.append(list(messages))
        human_text = next(
            (str(m.content) for m in reversed(messages) if getattr(m, "type", "") == "human"),
            "",
        )
        match = re.search(r"### Current Node\s*\n\s*`([A-Za-z0-9.\-_]+)`", human_text)
        if match is None:
            raise RuntimeError(
                "_NodeRoutedFauxModel could not find '### Current Node' in the prompt; "
                f"routing is impossible. Prompt head: {human_text[:200]!r}"
            )
        node_id = match.group(1)
        if node_id not in self._scripts:
            raise RuntimeError(f"No faux script registered for node {node_id}.")
        if not self._scripts[node_id]:
            raise RuntimeError(f"Faux script for node {node_id} ran out of scripted turns.")
        self._routed[node_id] = self._routed.get(node_id, 0) + 1
        return ChatResult(
            generations=[ChatGeneration(message=self._scripts[node_id].pop(0))]
        )


class _BaselineRedHandler(FakeAppHandler):
    """App handler whose runs are RED the first time a file executes.

    The parallel drain interleaves nodes, so a static FIFO of results cannot
    be aligned with callers. What the compile actually needs per test file is
    order-dependent, not global-order-dependent: the DESIGN baseline is the
    first execution of the freshly generated file (must be RED), and every
    later execution (the IMPLEMENT agent's run_tests) passes. Keying on the
    file makes the script deterministic under any interleaving.
    """

    def __init__(self) -> None:
        super().__init__()
        self._run_files: set[str] = set()

    async def run_test_group(
        self,
        test_type: str,
        file_paths: list[str],
        web_port: int | None = None,
    ) -> TestRunResult:
        key = "|".join(file_paths)
        first_run = key not in self._run_files
        self._run_files.add(key)
        self.calls.append((test_type, list(file_paths)))
        return parse_test_run(failing_test_output() if first_run else passing_test_output())


# ---------------------------------------------------------------------------
# Workspace: a real git repo (the drain opens real worktrees and merges back).
# ---------------------------------------------------------------------------


def _make_git_workspace(parent: Path) -> Path:
    workspace = parent / "workspace"
    workspace.mkdir(parents=True)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "test"],
        ["config", "core.autocrlf", "false"],
    ):
        subprocess.run(["git", *args], cwd=str(workspace), check=True, capture_output=True)
    (workspace / "backend").mkdir()
    (workspace / "frontend").mkdir()
    (workspace / ".gitignore").write_text(
        "# >>> arcbench-agent-runtime >>>\n.arc/*\n!.arc/traceability/\n!.arc/traceability/**\n"
        "# <<< arcbench-agent-runtime <<<\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=str(workspace), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=str(workspace), check=True, capture_output=True
    )
    return workspace


@pytest.fixture
def keep_req2_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Process-wide runtime bound to the git workspace (the drain merges into
    it and reads traceability through it)."""
    workspace = _make_git_workspace(tmp_path)
    monkeypatch.setattr(core_config, "_workspace_root", workspace)
    monkeypatch.setenv("ARC_WORKSPACE_ROOT", str(workspace))
    runtime = configure_runtime(project_dir=str(workspace))
    # initialize_project normally does this; the e2e skips that entry, so the
    # seven-table store is laid out here exactly as a real compile would.
    runtime.traceability.init_store(reset=False)
    yield workspace, runtime
    reset_runtime_for_tests()


def test_keep_req2_faux_compile_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keep_req2_runtime,
) -> None:
    # Parallel worktree mode with the depth-2 split is the code path under
    # test, drained at 2 real slots: that is the minimum that opens issue
    # #91's window (a group worktree's `git checkout -B ... master` in
    # `prepare` running concurrently with another group's `git merge` on the
    # integration branch). Before the fix the checkout's `check=False`
    # swallowed the contended write, the index held files the disk never
    # materialized, the task's `git add -A .` staged their deletion, and a
    # later sibling's merge hit a modify/delete conflict (2/3 runs failed at
    # 2 slots, 3/3 at 3). The manager's integration gate now excludes
    # prepare/reset from integrate, so 2 slots is also a regression pin; the
    # deterministic gate-contract tests live in test_worktree_manager.py.
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_AFFINITY_DEPTH", "2")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    monkeypatch.setenv("ARC_AUTO_TDD_RETRY", "0")
    monkeypatch.setenv("ARC_VISUAL_PRECOMPUTE", "0")
    # No arbitration in this run: the faux scripts never drift their anchors,
    # and an inherited ARC_MERGE_ARBITRATION from the host environment must
    # not activate the escalation path mid-e2e.
    monkeypatch.delenv("ARC_MERGE_ARBITRATION", raising=False)
    workspace, runtime = keep_req2_runtime

    tree = load_requirements(KEEP_REQ2_YAML)
    nodes = _walk_nodes(tree)
    model = _NodeRoutedFauxModel()
    model.set_node_scripts(
        {node["id"]: _node_script(node, is_non_leaf) for node, is_non_leaf in nodes}
    )
    fake_handler = _BaselineRedHandler()

    manager = ARCWorkflowManager(
        workspace_path=str(workspace),
        requirement_path=str(KEEP_REQ2_YAML),
        app_type="web",
        web_port=4200,
        log_cb=lambda *args, **kwargs: None,
    )
    # compile_requirement_tree is the drain entry; initialize_project (npm
    # template setup) is not under test, so the fixture's runtime is wired
    # directly.
    manager.runtime = runtime

    # The drain builds one runner per task; inject the faux model and the
    # scripted handler at that seam (the serial-mode shared runner too).
    original_builder = ARCWorkflowManager._build_task_phase_runner

    def build_with_faux(self, workspace_path: str, web_port: int | None):
        runner = original_builder(self, workspace_path, web_port)
        runner.interface_designer.model = model
        runner.test_generator.model = model
        runner.test_driven_developer.model = model
        runner.app_handler = fake_handler
        runner.test_driven_developer.app_handler = fake_handler
        return runner

    monkeypatch.setattr(ARCWorkflowManager, "_build_task_phase_runner", build_with_faux)
    manager.interface_designer.model = model
    manager.test_generator.model = model
    manager.test_driven_developer.model = model
    manager.phase_runner.app_handler = fake_handler
    manager.phase_runner.test_driven_developer.app_handler = fake_handler

    result = asyncio.run(manager.compile_requirement_tree(tree))

    assert result["ok"] is True, result
    assert result["failed_nodes"] == []
    assert result["blocked_nodes"] == []
    assert result["unvalidated_tasks"] == []

    states: dict[str, str] = result["states"]
    non_leaf_ids = {node["id"] for node, is_non_leaf in nodes if is_non_leaf}
    assert len(states) == 31
    for node_id, state in states.items():
        assert state in {NODE_PASSED, NODE_CONVERGED}, (node_id, state)
        if node_id in non_leaf_ids:
            assert state == NODE_CONVERGED, (node_id, state)
    # 8 non-leaf nodes: ROOT, REQ-2, 2.3, 2.5, 2.6, 2.7, 2.7.6, 2.8.
    assert non_leaf_ids == {"ROOT", "REQ-2", "REQ-2.3", "REQ-2.5", "REQ-2.6", "REQ-2.7", "REQ-2.7.6", "REQ-2.8"}
    assert sum(1 for s in states.values() if s == NODE_CONVERGED) == 8

    # Every leaf's script was consumed exactly; non-leaf nodes never touch
    # the model (DESIGN skip path + direct IMPLEMENT completion).
    assert model.pending_counts() == {}, model.pending_counts()
    assert set(model.routed_nodes()) == {
        node["id"] for node, is_non_leaf in nodes if not is_non_leaf
    }

    # Durable .arc/ artifacts: the queue with the split affinity map and the
    # seven traceability tables.
    arc = workspace / ".arc"
    queue = json.loads((arc / "processing_queue.json").read_text(encoding="utf-8"))
    affinity = queue["affinity"]
    for feature in FEATURE_SUBTREES:
        assert affinity[feature] == feature
    traceability = arc / "traceability"
    for table in (
        "requirements",
        "scenarios",
        "interfaces",
        "tests",
        "call_edges",
        "node_states",
        "node_contracts",
    ):
        assert (traceability / f"{table}.json").is_file(), table
    # 23 leaves each registered one unit test against one owned contract;
    # non-leaf nodes take the DESIGN skip path and own no interfaces.
    tests = json.loads((traceability / "tests.json").read_text(encoding="utf-8"))
    assert len(tests) == 23
    interfaces = json.loads((traceability / "interfaces.json").read_text(encoding="utf-8"))
    assert len(interfaces) == 23

    # The merged integration workspace carries every node's implementation
    # (node ids carry dots, e.g. REQ-2.1 -> req_2.1.js).
    for slug in ("req_2.1", "req_2.8.3", "req_2.7.6.2"):
        assert (workspace / "backend" / "src" / "features" / f"{slug}.js").is_file(), slug


def test_keep_req2_pipeline_mode_starts_dependent_design_before_dependency_implement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keep_req2_runtime,
) -> None:
    """Issue #83's scheduling deliverable on the real fixture: with
    ``ARC_DESIGN_GATE_PIPELINE`` on, a dependent's DESIGN is schedulable while
    its declared dependency's IMPLEMENT is still pending - it starts the
    moment the dependency's DESIGN completes, no longer queued behind the
    dependency's landing IMPLEMENT.

    Why the assertion watches scheduling decisions, not wall-clock start
    order: the serial drain (``ARC_MAX_CONCURRENT_TASKS=1``, kept serial so
    the flat queue order stays deterministic) picks tasks in
    flat queue order, and every dependency pair inside the fixture shares the
    REQ-2 affinity subtree - so even when the dependent's DESIGN is runnable
    first, the flat order may start the dependency's IMPLEMENT before it. The
    observable contract under serial draining is therefore *which tasks were
    runnable when*: each time the drain picks a task, the pipelined gate must
    already consider the dependent's DESIGN runnable while the dependency's
    IMPLEMENT has not completed - and the closed gate must not (that
    difference is exactly the pipelining lever). A parallel-drain variant
    asserting the interleaved start order lives in
    test_design_gate_pipelining.py (stubbed phases, no #91 exposure)."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_AFFINITY_DEPTH", "2")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "1")
    monkeypatch.setenv("ARC_AUTO_TDD_RETRY", "0")
    monkeypatch.setenv("ARC_VISUAL_PRECOMPUTE", "0")
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    # Same isolation as the end-to-end test above: no arbitration here.
    monkeypatch.delenv("ARC_MERGE_ARBITRATION", raising=False)
    workspace, runtime = keep_req2_runtime

    tree = load_requirements(KEEP_REQ2_YAML)
    nodes = _walk_nodes(tree)
    model = _NodeRoutedFauxModel()
    model.set_node_scripts(
        {node["id"]: _node_script(node, is_non_leaf) for node, is_non_leaf in nodes}
    )
    fake_handler = _BaselineRedHandler()

    manager = ARCWorkflowManager(
        workspace_path=str(workspace),
        requirement_path=str(KEEP_REQ2_YAML),
        app_type="web",
        web_port=4200,
        log_cb=lambda *args, **kwargs: None,
    )
    manager.runtime = runtime

    # Scheduling-decision log: every time the drain begins a task, record
    # whether REQ-2.2's DESIGN was already runnable (its dependency REQ-2.1's
    # DESIGN done, IMPLEMENT not yet) at that moment.
    pipeline_runnable_before_dependency_implement = False
    original_begin = ARCWorkflowManager._begin_task

    def begin_and_observe(self, task: dict, queue_state: dict) -> None:
        nonlocal pipeline_runnable_before_dependency_implement
        if (
            task["node_id"] == "REQ-2.1"
            and task["phase"] == "IMPLEMENT"
            and not pipeline_runnable_before_dependency_implement
        ):
            rb_design = next(
                (
                    other
                    for other in queue_state["tasks"]
                    if other["node_id"] == "REQ-2.2" and other["phase"] == "DESIGN"
                ),
                None,
            )
            if rb_design is not None and self._task_dependencies_met(queue_state, rb_design):
                pipeline_runnable_before_dependency_implement = True
        original_begin(self, task, queue_state)

    original_builder = ARCWorkflowManager._build_task_phase_runner

    def build_with_faux(self, workspace_path: str, web_port: int | None):
        runner = original_builder(self, workspace_path, web_port)
        runner.interface_designer.model = model
        runner.test_generator.model = model
        runner.test_driven_developer.model = model
        runner.app_handler = fake_handler
        runner.test_driven_developer.app_handler = fake_handler
        return runner

    monkeypatch.setattr(ARCWorkflowManager, "_build_task_phase_runner", build_with_faux)
    monkeypatch.setattr(ARCWorkflowManager, "_begin_task", begin_and_observe)
    manager.interface_designer.model = model
    manager.test_generator.model = model
    manager.test_driven_developer.model = model
    manager.phase_runner.app_handler = fake_handler
    manager.phase_runner.test_driven_developer.app_handler = fake_handler

    result = asyncio.run(manager.compile_requirement_tree(tree))

    assert result["ok"] is True, result
    assert pipeline_runnable_before_dependency_implement, (
        "pipelined mode: REQ-2.2's DESIGN must be runnable while REQ-2.1's "
        "IMPLEMENT has not completed (the dependency's DESIGN gating it has "
        "already merged)"
    )

    # The serial flat order still lands REQ-2.2's IMPLEMENT after REQ-2.1's
    # (the IMPLEMENT gate is unchanged); the durable queue is the evidence.
    arc = workspace / ".arc"
    queue = json.loads((arc / "processing_queue.json").read_text(encoding="utf-8"))
    tasks = {task["task_id"]: task for task in queue["tasks"]}
    assert tasks["REQ-2.1:IMPLEMENT"]["status"] == "COMPLETED"
    assert tasks["REQ-2.2:IMPLEMENT"]["status"] == "COMPLETED"
    assert tasks["REQ-2.2:DESIGN"]["status"] == "COMPLETED"
