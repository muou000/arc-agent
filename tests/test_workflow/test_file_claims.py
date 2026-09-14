"""Unit tests for the cross-node file-claim machinery (core/file_claims.py).

The claim registry prevents parallel worktree tasks from both creating the
same new file (an add/add merge conflict no resolver can fix). These tests
cover the registry itself, the per-agent gate (including its two-root
semantics: shared integration workspace for the registry, per-agent worktree
for the tracked check), the path normalisation, and the
StageDisciplineMiddleware integration that rejects a sibling's claimed path
at write time.
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
    tracked: set[str] | None = frozenset(),  # type: ignore[assignment]
    agent_root: str = "unused",
) -> FileClaimGate:
    """Gate with the git snapshot stubbed as a fixed tracked set.

    ``tracked=None`` makes the loader fail open (git unavailable): every
    path is treated as tracked, so nothing is claimed or blocked.
    """

    def tracked_loader(_root: str) -> set[str] | None:
        return tracked

    return FileClaimGate(
        registry,
        node_id=node_id,
        agent_root=agent_root,
        tracked_loader=tracked_loader,
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


def test_registry_persists_at_mutation_endpoints_not_per_claim(tmp_path) -> None:
    """The on-disk snapshot is written at release/reset only: the per-write
    hot path stays in-process (a per-claim rewrite would re-serialise the
    whole JSON on every file write of every agent)."""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    first = FileClaimRegistry(str(workspace))
    first.claim("kept/api.ts", "REQ-2")
    first.claim("freed/api.ts", "REQ-1")

    # A claim alone must not touch the disk...
    assert not (workspace / ".arc" / "file_claims.json").exists()
    # ...while a release snapshots the surviving claims.
    first.release_node("REQ-1")
    assert (workspace / ".arc" / "file_claims.json").exists()

    second = FileClaimRegistry(str(workspace))
    assert second.claim("freed/api.ts", "REQ-3") is None, "the release survived"
    assert second.claim("kept/api.ts", "REQ-3") == "REQ-2", "the surviving claim survived"


def test_registry_never_creates_a_placeholder_workspace_root(tmp_path) -> None:
    workspace = tmp_path / "placeholder"
    registry = FileClaimRegistry(str(workspace))
    registry.claim("any.ts", "REQ-1")
    registry.reset()
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
# gate: two-root semantics (integration workspace vs per-agent worktree)
# ---------------------------------------------------------------------------


def test_gate_claims_untracked_path_for_owner_then_blocks_sibling() -> None:
    registry = FileClaimRegistry("unused-root")
    auth_api = "frontend/src/features/auth/authApi.ts"
    gate_a = make_gate(registry, "REQ-1", tracked=set())
    gate_b = make_gate(registry, "REQ-2", tracked=set())

    assert gate_a.check_and_claim(f"/workspace/{auth_api}") is None
    blocked = gate_b.check_and_claim(f"/workspace/{auth_api}")
    assert blocked is not None and "parallel node REQ-1" in blocked

    # The tracked snapshot is loaded once per gate; repeated checks stay cheap.
    assert gate_b.check_and_claim(auth_api) is not None


def test_gate_ignores_tracked_paths_even_with_sibling_claim() -> None:
    registry = FileClaimRegistry("unused-root")
    gate_a = make_gate(registry, "REQ-1", tracked=set())
    gate_b = make_gate(registry, "REQ-2", tracked={"backend/src/app.js"})

    assert gate_a.check_and_claim("/workspace/backend/src/app.js") is None
    # app.js is git-tracked, so REQ-2 writes to it are governed by the shared
    # surface rules, not by claims - even though REQ-1 wrote it earlier.
    assert gate_b.check_and_claim("/workspace/backend/src/app.js") is None


def test_gate_ignores_non_workspace_paths() -> None:
    registry = FileClaimRegistry("unused-root")
    gate = make_gate(registry, "REQ-1", tracked=set())
    assert gate.check_and_claim("/skills/auth-session-consistency/SKILL.md") is None
    assert registry.claim("skills", "REQ-9") is None, "no claim key was created"


def test_gate_fails_open_when_git_is_unavailable() -> None:
    registry = FileClaimRegistry("unused-root")
    gate_a = make_gate(registry, "REQ-1", tracked=None)
    gate_b = make_gate(registry, "REQ-2", tracked=None)
    path = "/workspace/frontend/new.ts"

    assert gate_a.check_and_claim(path) is None, "git failure must never block writes"
    assert gate_b.check_and_claim(path) is None, "and never record claims either"


def test_gate_tracked_check_uses_literal_membership_not_pathspec_globs() -> None:
    """Next.js-style dynamic-route segments (``[id]``) are glob-special for
    git pathspecs; the tracked check must stay literal so a tracked file is
    never misread as untracked."""
    registry = FileClaimRegistry("unused-root")
    dynamic_route = "frontend/src/app/posts/[id]/page.tsx"
    gate = make_gate(registry, "REQ-1", tracked={dynamic_route})

    assert gate.check_and_claim(f"/workspace/{dynamic_route}") is None, "tracked stays tracked"
    assert registry.claim(dynamic_route, "REQ-2") is None, "no claim key was created"


def test_gate_arbitrates_sibling_work_across_two_worktrees() -> None:
    """Two gates share one registry (integration workspace) but carry
    different agent roots (task worktrees). A sibling's committed-but-
    unmerged new file is untracked in this worktree, so both may try it -
    the first claim wins and the sibling is blocked. After the winner
    merges, the path is tracked in a freshly prepared worktree, so a new
    gate for the loser treats it as merged sibling work instead."""
    registry = FileClaimRegistry("unused-root")
    auth_api = "frontend/src/features/auth/authApi.ts"

    # Worktrees prepared from the same integration HEAD: template files only.
    worktree_a = make_gate(registry, "REQ-A", tracked={"backend/src/app.js"}, agent_root="wtA")
    worktree_b = make_gate(registry, "REQ-B", tracked={"backend/src/app.js"}, agent_root="wtB")

    assert worktree_b.check_and_claim(auth_api) is None, "REQ-B claims the new file first"
    blocked = worktree_a.check_and_claim(f"/workspace/{auth_api}")
    assert blocked is not None and "parallel node REQ-B" in blocked

    # REQ-A's retry worktree is prepared after REQ-B merged: the path is now
    # tracked there, so REQ-A's write goes through the merge rules instead.
    worktree_a2 = make_gate(
        registry, "REQ-A", tracked={"backend/src/app.js", auth_api}, agent_root="wtA2"
    )
    assert worktree_a2.check_and_claim(auth_api) is None


# ---------------------------------------------------------------------------
# middleware integration
# ---------------------------------------------------------------------------


def test_middleware_blocks_write_to_sibling_claimed_new_file() -> None:
    registry = FileClaimRegistry("unused-root")
    auth_api = "frontend/src/features/auth/authApi.ts"
    write = {"file_path": f"/workspace/{auth_api}", "content": "export {}\n"}
    middleware_a = StageDisciplineMiddleware(
        stage="interface_design",
        file_claim_gate=make_gate(registry, "REQ-1", tracked=set()),
    )
    middleware_b = StageDisciplineMiddleware(
        stage="interface_design",
        file_claim_gate=make_gate(registry, "REQ-2", tracked=set()),
    )

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
        file_claim_gate=make_gate(registry, "REQ-1", tracked=set()),
    )
    big_write = {"file_path": "/workspace/frontend/big.ts", "content": "\n".join(f"line {i}" for i in range(400))}
    result = middleware.wrap_tool_call(make_request("write_file", big_write), ok_tool)
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert "small skeletons" in result.content
    assert registry.claim("frontend/big.ts", "REQ-2") is None, "the rejected write claimed nothing"
