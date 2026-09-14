"""Unit tests for the cross-node file-claim machinery (core/file_claims.py).

The claim registry prevents parallel worktree tasks from both creating the
same new file (an add/add merge conflict no resolver can fix). These tests
cover the registry itself, the per-agent gate, the path normalisation, and
the StageDisciplineMiddleware integration that rejects a sibling's claimed
path at write time.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from agents.runtime.stage_discipline import BLOCKED_RESULT_PREFIX, StageDisciplineMiddleware
from core.file_claims import (
    FileClaimGate,
    FileClaimRegistry,
    get_file_claim_registry,
    normalize_claim_path,
)


def make_request(name: str, args: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(tool_call={"name": name, "args": args, "id": "call-1"}, tool=None, state={}, runtime=None)


def ok_tool(request: ToolCallRequest) -> ToolMessage:
    return ToolMessage(content="ok", name=request.tool_call["name"], tool_call_id=request.tool_call["id"])


def make_gate(
    registry: FileClaimRegistry,
    node_id: str,
    *,
    untracked: set[str],
) -> FileClaimGate:
    def tracked_check(_root: str, rel_path: str) -> bool:
        return rel_path not in untracked

    return FileClaimGate(
        registry,
        node_id=node_id,
        agent_root="unused",
        tracked_check=tracked_check,
    )


# ---------------------------------------------------------------------------
# path normalisation
# ---------------------------------------------------------------------------


def test_normalize_claim_path_accepts_workspace_forms() -> None:
    for raw in (
        "/workspace/frontend/src/api.ts",
        "/frontend/src/api.ts",
        "frontend/src/api.ts",
        "\\workspace\\frontend\\src\\api.ts",
        "frontend\\src\\api.ts",
    ):
        assert normalize_claim_path(raw) == "frontend/src/api.ts", raw


def test_normalize_claim_path_rejects_non_workspace_roots() -> None:
    for raw in ("", "/skills/auth-session-consistency/SKILL.md", "/skills", "/workspace"):
        assert normalize_claim_path(raw) == "", raw


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_registry_claim_conflict_release_and_reset(tmp_path) -> None:
    registry = FileClaimRegistry(str(tmp_path / "ws"))

    assert registry.claim("frontend/auth.ts", "REQ-1") is None
    assert registry.claim("frontend/auth.ts", "REQ-1") is None, "self-claims stay allowed"
    assert registry.claim("frontend/auth.ts", "REQ-2") == "REQ-1"

    freed = registry.release_node("REQ-1")
    assert freed == ["frontend/auth.ts"]
    assert registry.claim("frontend/auth.ts", "REQ-2") is None, "released paths are free"

    registry.claim("other.js", "REQ-3")
    registry.reset()
    assert registry.claim("other.js", "REQ-4") is None


def test_registry_persists_across_instances(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    first = FileClaimRegistry(str(workspace))
    first.claim("shared/api.ts", "REQ-1")

    second = FileClaimRegistry(str(workspace))
    assert second.claim("shared/api.ts", "REQ-2") == "REQ-1"


def test_registry_never_creates_a_placeholder_workspace_root(tmp_path) -> None:
    workspace = tmp_path / "placeholder"
    registry = FileClaimRegistry(str(workspace))
    registry.claim("any.ts", "REQ-1")
    assert not workspace.exists(), "the registry must not fabricate its workspace root"


def test_registry_tolerates_corrupt_state_file(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    (workspace / ".arc").mkdir()
    (workspace / ".arc" / "file_claims.json").write_text("{not json", encoding="utf-8")

    registry = FileClaimRegistry(str(workspace))
    assert registry.claim("any.ts", "REQ-1") is None


def test_registry_singleton_is_per_workspace(tmp_path) -> None:
    left = get_file_claim_registry(str(tmp_path / "left"))
    right = get_file_claim_registry(str(tmp_path / "right"))
    assert left is not right
    assert get_file_claim_registry(str(tmp_path / "left")) is left


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------


def test_gate_claims_untracked_path_for_owner_then_blocks_sibling() -> None:
    registry = FileClaimRegistry("unused-root")
    untracked = {"frontend/src/features/auth/authApi.ts"}
    gate_a = make_gate(registry, "REQ-1", untracked=untracked)
    gate_b = make_gate(registry, "REQ-2", untracked=untracked)

    assert gate_a.check_and_claim("/workspace/frontend/src/features/auth/authApi.ts") is None
    blocked = gate_b.check_and_claim("/workspace/frontend/src/features/auth/authApi.ts")
    assert blocked is not None and "parallel node REQ-1" in blocked

    # The untracked status is cached per gate, so repeated checks stay cheap.
    assert gate_b.check_and_claim("frontend/src/features/auth/authApi.ts") is not None


def test_gate_ignores_tracked_paths_even_with_sibling_claim() -> None:
    registry = FileClaimRegistry("unused-root")
    gate_a = make_gate(registry, "REQ-1", untracked=set())
    gate_b = make_gate(registry, "REQ-2", untracked=set())

    assert gate_a.check_and_claim("/workspace/backend/src/app.js") is None
    # app.js is git-tracked, so REQ-2 writes to it are governed by the shared
    # surface rules, not by claims - even if REQ-1 wrote it earlier.
    assert gate_b.check_and_claim("/workspace/backend/src/app.js") is None
    assert registry.claim("backend/src/app.js", "REQ-3") is None, "tracked paths never enter the registry"


def test_gate_ignores_non_workspace_paths() -> None:
    registry = FileClaimRegistry("unused-root")
    gate = make_gate(registry, "REQ-1", untracked=set())
    assert gate.check_and_claim("/skills/auth-session-consistency/SKILL.md") is None
    assert registry.claim("skills", "REQ-9") is None, "no claim key was created"


# ---------------------------------------------------------------------------
# middleware integration
# ---------------------------------------------------------------------------


def test_middleware_blocks_write_to_sibling_claimed_new_file() -> None:
    registry = FileClaimRegistry("unused-root")
    untracked = {"frontend/src/features/auth/authApi.ts"}
    middleware_a = StageDisciplineMiddleware(
        stage="interface_design",
        file_claim_gate=make_gate(registry, "REQ-1", untracked=untracked),
    )
    middleware_b = StageDisciplineMiddleware(
        stage="interface_design",
        file_claim_gate=make_gate(registry, "REQ-2", untracked=untracked),
    )
    write = {"file_path": "/workspace/frontend/src/features/auth/authApi.ts", "content": "export {}\n"}

    first = middleware_a.wrap_tool_call(make_request("write_file", write), ok_tool)
    assert isinstance(first, ToolMessage) and first.content == "ok"

    second = middleware_b.wrap_tool_call(make_request("write_file", write), ok_tool)
    assert isinstance(second, ToolMessage) and second.status == "error"
    assert second.content.startswith(BLOCKED_RESULT_PREFIX)
    assert "parallel node REQ-1" in second.content


def test_middleware_without_gate_keeps_historical_behaviour() -> None:
    middleware = StageDisciplineMiddleware(stage="implementation")
    result = middleware.wrap_tool_call(
        make_request("write_file", {"file_path": "/workspace/any/new/file.ts", "content": "x\n"}),
        ok_tool,
    )
    assert isinstance(result, ToolMessage) and result.content == "ok"


def test_discipline_rejection_does_not_claim_the_path() -> None:
    registry = FileClaimRegistry("unused-root")
    # The path is untracked, but the interface_design skeleton-line limit
    # rejects the write before the claim gate records ownership.
    middleware = StageDisciplineMiddleware(
        stage="interface_design",
        file_claim_gate=make_gate(registry, "REQ-1", untracked={"frontend/big.ts"}),
    )
    big_write = {"file_path": "/workspace/frontend/big.ts", "content": "\n".join(f"line {i}" for i in range(400))}
    result = middleware.wrap_tool_call(make_request("write_file", big_write), ok_tool)
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert "small skeletons" in result.content
    assert registry.claim("frontend/big.ts", "REQ-2") is None, "the rejected write claimed nothing"
