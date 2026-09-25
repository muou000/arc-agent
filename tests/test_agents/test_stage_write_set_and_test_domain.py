"""Focused ownership rails for the ADR 0008 stage-pipeline slice (#256)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from agents.context.prompts import interface_designer, test_driven_developer, test_generator
from agents.runtime.capabilities import (
    capability_for,
    is_node_test_path,
    node_test_namespace,
    node_test_namespace_hint,
    stable_node_path_segment,
)
from agents.runtime.stage_discipline import StageDisciplineMiddleware
from agents.tools.declaration_budget import ESCALATION_THRESHOLD
from agents.tools.stage_write_set import StageWriteSetLock, build_declare_stage_write_set_tool
from agents.tools.test_manifest import (
    DeclaredTestFile,
    TestManifestLock,
    TestManifestOwnershipRegistry,
    build_declare_test_manifest_tool,
)
from core.scheduling import stage_write_sets_disjoint


def _request(name: str, args: dict[str, Any], call_id: str = "call-1") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args, "id": call_id},
        tool=None,
        state={},
        runtime=None,
    )


def _ok(request: ToolCallRequest) -> ToolMessage:
    return ToolMessage(content="ok", name=request.tool_call["name"], tool_call_id=request.tool_call["id"])


def _content(result: ToolMessage | Any) -> str:
    assert isinstance(result, ToolMessage)
    return str(result.content)


def test_node_namespace_is_stable_and_collision_resistant() -> None:
    segment = stable_node_path_segment("REQ-1")
    assert segment.isidentifier()
    assert node_test_namespace("REQ-1") == f"tests/generated/{segment}"
    assert is_node_test_path(f"backend/tests/generated/{segment}/unit/login.test.ts", "REQ-1")
    sibling = stable_node_path_segment("REQ-2")
    assert not is_node_test_path(f"backend/tests/generated/{sibling}/unit/login.test.ts", "REQ-1")
    assert not is_node_test_path(f"backend/tests/generated/{segment}/../{sibling}/login.test.ts", "REQ-1")
    assert stable_node_path_segment("A/B") != stable_node_path_segment("A_B")
    assert stable_node_path_segment("REQ-1") != stable_node_path_segment("req-1")


def test_stage_write_set_must_be_declared_and_is_immutable() -> None:
    lock = StageWriteSetLock(stage="implementation")
    middleware = StageDisciplineMiddleware(
        stage="implementation",
        stage_write_set_lock=lock,
    )

    blocked = middleware.wrap_tool_call(
        _request("write_file", {"file_path": "/workspace/src/app.py", "content": "pass\n"}),
        _ok,
    )
    assert "declare the complete stage write set" in _content(blocked)

    tool = build_declare_stage_write_set_tool(stage="implementation", lock=lock)
    payload = asyncio.run(tool(paths=["src/app.py"]))
    assert '"status": "locked"' in payload

    allowed = middleware.wrap_tool_call(
        _request("write_file", {"file_path": "/workspace/src/app.py", "content": "pass\n"}),
        _ok,
    )
    assert not isinstance(allowed, ToolMessage) or allowed.status != "error"

    drift = asyncio.run(tool(paths=["src/other.py"]))
    assert '"status": "error"' in drift
    assert "already locked" in drift

    outside = middleware.wrap_tool_call(
        _request("write_file", {"file_path": "/workspace/src/other.py", "content": "pass\n"}),
        _ok,
    )
    assert "was not declared" in _content(outside)


def test_stage_write_set_rejects_sibling_test_and_shared_paths() -> None:
    lock = StageWriteSetLock(stage="test_generation", node_id="REQ-1")
    assert lock.declare(["backend/tests/generated/REQ-2/unit/sibling.test.ts"]) is not None
    assert lock.declare(["backend/vitest.config.js"]) is not None
    assert lock.declare(
        [f"backend/tests/generated/{stable_node_path_segment('REQ-1')}/unit/current.test.ts"]
    ) is None


def test_staged_test_helpers_require_write_set_but_not_manifest() -> None:
    node_id = "REQ-1"
    helper = f"backend/tests/generated/{stable_node_path_segment(node_id)}/unit/support.ts"
    shared_paths = (
        "backend/vitest.config.js",
        "backend/playwright.config.js",
        "frontend/vite.config.js",
        "frontend/test/setup.ts",
    )
    for stage in ("test_generation", "implementation"):
        write_set = StageWriteSetLock(stage=stage, node_id=node_id)
        manifest = TestManifestLock(node_id=node_id, enforce_node_namespace=True)
        middleware = StageDisciplineMiddleware(
            stage=stage,
            node_id=node_id,
            enforce_node_test_domain=True,
            stage_write_set_lock=write_set,
            test_manifest_lock=manifest if stage == "test_generation" else None,
        )

        for shared in shared_paths:
            assert "read-only" in (write_set.declare([shared]) or "")
            assert "Shared test resource blocked" in _content(
                middleware.wrap_tool_call(
                    _request("write_file", {"file_path": f"/workspace/{shared}", "content": "x"}), _ok
                )
            )
        assert "declare the complete stage write set" in _content(
            middleware.wrap_tool_call(
                _request("write_file", {"file_path": f"/workspace/{helper}", "content": "x"}), _ok
            )
        )
        assert write_set.declare([helper]) is None
        allowed = middleware.wrap_tool_call(
            _request("write_file", {"file_path": f"/workspace/{helper}", "content": "x"}), _ok
        )
        assert _content(allowed) == "ok"

        if stage == "test_generation":
            tool = build_declare_test_manifest_tool(node_id=node_id, manifest_lock=manifest)
            assert "does not look like a test file" in asyncio.run(
                tool(files=[{"file_path": helper, "type": "Unit", "interface_ids": []}])
            )
            for shared in shared_paths:
                assert '"status": "error"' in asyncio.run(
                    tool(files=[{"file_path": shared, "type": "Unit", "interface_ids": []}])
                )


def test_tdd_prompt_and_repair_skill_hand_off_shared_config_failures() -> None:
    system_prompt = test_driven_developer.get_system_prompt()
    task_prompt = test_driven_developer.get_user_prompt(
        node_id="REQ-1", dynamic_context="", test_files=[], test_type="Unit", node_tests=[]
    )
    skill = (
        Path(__file__).resolve().parents[2] / "skills" / "tdd-test-failure-repair" / "SKILL.md"
    ).read_text(encoding="utf-8")

    for visible_text in (system_prompt, task_prompt, skill):
        assert "pipeline" in visible_text.lower()
        assert "shared runner configuration" in visible_text.lower()
        assert "read-only" in visible_text
        assert "coordinator/template" in visible_text
        assert "failure fingerprint" in visible_text
        assert "package scripts" in visible_text
        assert "current node" in visible_text.lower()
    assert "When the stage pipeline is active, shared runner configuration" in system_prompt
    assert "in pipeline mode hand off faults" in task_prompt


def test_legacy_manifest_rejection_does_not_require_unmounted_write_set_tool() -> None:
    middleware = StageDisciplineMiddleware(
        stage="test_generation", test_manifest_lock=TestManifestLock()
    )
    blocked = middleware.wrap_tool_call(
        _request("write_file", {"file_path": "/workspace/tests/login.test.js", "content": "x"}), _ok
    )
    assert "Manifest-first blocked" in _content(blocked)
    assert "stage write-set declaration" not in _content(blocked)


def test_scheduler_fails_closed_for_invalid_test_write_sets() -> None:
    left = {
        "node_id": "REQ-1",
        "stage": "TEST_GENERATION",
        "declared_write_set": ["backend/tests/generated/REQ-2/unit/sibling.test.ts"],
        "enforce_node_test_domain": True,
    }
    right = {
        "node_id": "REQ-2",
        "stage": "INTERFACE_DESIGN",
        "declared_write_set": ["src/feature.ts"],
    }
    assert not stage_write_sets_disjoint(left, right)


def test_shared_runner_resources_are_read_only_even_when_declared() -> None:
    lock = StageWriteSetLock(stage="test_generation")
    lock.declare(["backend/vitest.config.js"])
    middleware = StageDisciplineMiddleware(
        stage="test_generation",
        stage_write_set_lock=lock,
    )
    result = middleware.wrap_tool_call(
        _request(
            "edit_file",
            {
                "file_path": "/workspace/backend/vitest.config.js",
                "old_string": "include",
                "new_string": "include",
            },
        ),
        _ok,
    )
    assert "Shared test resource blocked" in _content(result)
    assert not capability_for(
        "implementation",
        "write_file",
        "/workspace/backend/vitest.config.js",
        enforce_node_test_domain=True,
    ).allowed


def test_tdd_can_repair_current_node_test_but_not_sibling_test() -> None:
    current = f"backend/tests/generated/{stable_node_path_segment('REQ-1')}/unit/current.test.ts"
    sibling = f"backend/tests/generated/{stable_node_path_segment('REQ-2')}/unit/sibling.test.ts"
    manifest = TestManifestLock(
        declared_files={
            current: DeclaredTestFile(file_path=current, test_type="Unit"),
        },
        node_id="REQ-1",
        enforce_node_namespace=True,
    )
    write_set = StageWriteSetLock(stage="implementation")
    write_set.declare([current])
    middleware = StageDisciplineMiddleware(
        stage="implementation",
        node_id="REQ-1",
        enforce_node_test_domain=True,
        test_manifest_lock=manifest,
        stage_write_set_lock=write_set,
    )

    repaired = middleware.wrap_tool_call(
        _request(
            "edit_file",
            {"file_path": f"/workspace/{current}", "old_string": "old", "new_string": "new"},
        ),
        _ok,
    )
    assert not isinstance(repaired, ToolMessage) or repaired.status != "error"

    rejected = middleware.wrap_tool_call(
        _request(
            "edit_file",
            {"file_path": f"/workspace/{sibling}", "old_string": "old", "new_string": "new"},
        ),
        _ok,
    )
    assert "outside node `REQ-1`'s stable test namespace" in _content(rejected)


def test_manifest_ownership_claims_are_atomic_and_cross_node() -> None:
    registry = TestManifestOwnershipRegistry()
    first_lock = TestManifestLock()
    second_lock = TestManifestLock()
    first = build_declare_test_manifest_tool(
        node_id="REQ-1",
        manifest_lock=first_lock,
        ownership_registry=registry,
    )
    second = build_declare_test_manifest_tool(
        node_id="REQ-2",
        manifest_lock=second_lock,
        ownership_registry=registry,
    )
    declaration = [{"file_path": "tests/unit/shared.test.ts", "type": "Unit"}]

    assert '"status": "locked"' in asyncio.run(first(files=declaration))
    rejected = asyncio.run(second(files=declaration))
    assert '"status": "error"' in rejected
    assert "already claimed by node `REQ-1`" in rejected
    assert not second_lock.locked

    registry.release_node("REQ-1")
    assert '"status": "locked"' in asyncio.run(second(files=declaration))


def test_strict_manifest_rejects_shared_and_sibling_paths() -> None:
    lock = TestManifestLock(node_id="REQ-1", enforce_node_namespace=True)
    tool = build_declare_test_manifest_tool(node_id="REQ-1", manifest_lock=lock)

    shared = asyncio.run(tool(files=[{"file_path": "tests/fixtures/shared.test.ts", "type": "Unit"}]))
    assert '"status": "error"' in shared
    assert "shared test infrastructure" in shared

    sibling = asyncio.run(
        tool(files=[{"file_path": f"tests/generated/{stable_node_path_segment('REQ-2')}/unit/sibling.test.ts", "type": "Unit"}])
    )
    assert '"status": "error"' in sibling
    assert "outside node `REQ-1`'s stable test namespace" in sibling


def test_stage_prompts_name_the_write_set_and_node_domain_contract() -> None:
    assert "declare_stage_write_set" in interface_designer.get_system_prompt()
    assert "stable app-type namespace" in test_generator.get_system_prompt()
    assert "current node's test namespace" in test_driven_developer.get_system_prompt()


def test_namespace_hint_discloses_concrete_segment() -> None:
    hint = node_test_namespace_hint("REQ-1")
    segment = stable_node_path_segment("REQ-1")
    assert segment in hint
    assert "not the raw id" in hint
    for prefix in (
        "tests/generated",
        "frontend/tests/generated",
        "backend/tests/generated",
        "backend/test-e2e/generated",
    ):
        assert f"`{prefix}/{segment}/...`" in hint
    # The placeholder is exactly what the 2026-09-25 run's model guessed
    # around for 60+ calls; no model-facing surface may render it again.
    assert "<stable-node-id>" not in hint


def test_manifest_rejection_and_description_disclose_concrete_namespace() -> None:
    lock = TestManifestLock(node_id="REQ-1", enforce_node_namespace=True)
    tool = build_declare_test_manifest_tool(node_id="REQ-1", manifest_lock=lock)
    segment = stable_node_path_segment("REQ-1")
    assert segment in (tool.__doc__ or "")

    rejected = asyncio.run(
        tool(files=[{"file_path": "backend/tests/generated/req-1/probe.test.js", "type": "Unit"}])
    )
    assert '"status": "error"' in rejected
    assert "outside node `REQ-1`'s stable test namespace" in rejected
    assert segment in rejected
    assert "<stable-node-id>" not in rejected


def test_manifest_tool_description_omits_namespace_when_not_enforced() -> None:
    lock = TestManifestLock(node_id="REQ-1")
    tool = build_declare_test_manifest_tool(node_id="REQ-1", manifest_lock=lock)
    assert "stable test namespace segment" not in (tool.__doc__ or "")


def test_write_set_rejection_and_description_disclose_concrete_namespace() -> None:
    lock = StageWriteSetLock(stage="test_generation", node_id="REQ-1")
    tool = build_declare_stage_write_set_tool(stage="test_generation", lock=lock)
    segment = stable_node_path_segment("REQ-1")
    assert segment in (tool.__doc__ or "")

    rejected = lock.declare(["backend/tests/generated/req-1/probe.test.js"])
    assert rejected is not None
    assert "outside node `REQ-1`'s stable test namespace" in rejected
    assert segment in rejected
    assert "<stable-node-id>" not in rejected


def test_manifest_rejection_budget_escalates_then_resets_on_success() -> None:
    lock = TestManifestLock(node_id="REQ-1", enforce_node_namespace=True)
    tool = build_declare_test_manifest_tool(node_id="REQ-1", manifest_lock=lock)
    bad = [{"file_path": "backend/tests/generated/req-1/probe.test.js", "type": "Unit"}]

    pre_threshold = asyncio.run(tool(files=bad))
    assert "consecutive declarations" not in pre_threshold
    last = pre_threshold
    for _ in range(ESCALATION_THRESHOLD - 1):
        last = asyncio.run(tool(files=bad))
    assert "consecutive declarations" in last
    assert "declare_test_manifest" in last

    good = [
        {
            "file_path": f"backend/tests/generated/{stable_node_path_segment('REQ-1')}/probe.test.js",
            "type": "Unit",
            "coverage_scope": "owned",
        }
    ]
    assert '"status": "locked"' in asyncio.run(tool(files=good))

    again = asyncio.run(tool(files=bad))
    assert '"status": "error"' in again
    assert "consecutive declarations" not in again


def test_write_set_rejection_budget_escalates_then_resets_on_success() -> None:
    lock = StageWriteSetLock(stage="test_generation", node_id="REQ-1")
    tool = build_declare_stage_write_set_tool(stage="test_generation", lock=lock)
    bad = ["backend/tests/generated/req-1/probe.test.js"]

    payload = ""
    for _ in range(ESCALATION_THRESHOLD - 1):
        payload = asyncio.run(tool(paths=bad))
    assert '"status": "error"' in payload
    assert "consecutive declarations" not in payload
    payload = asyncio.run(tool(paths=bad))
    assert "consecutive declarations" in payload

    assert '"status": "locked"' in asyncio.run(tool(paths=["src/app.py"]))
    payload = asyncio.run(tool(paths=bad))
    assert '"status": "error"' in payload
    assert "consecutive declarations" not in payload
