"""Cross-node ownership registry for new files created in parallel worktrees.

Sibling requirement nodes compile in isolated git worktrees branched from the
same integration HEAD, so a file created by one node is invisible to the
others until its branch merges back. When two nodes both create the same
path, the second merge hits an add/add conflict that the additive resolver
explicitly refuses (a new file has no common base to append to) and the whole
node fails. This module prevents that class of failure at write time:

- ``FileClaimRegistry`` maps workspace-relative paths to the node id that
  first created them as a *new* (git-untracked) file. It is shared by every
  in-flight task in the process. The in-process map is the enforcement
  source of truth; the on-disk snapshot under
  ``<workspace>/.arc/file_claims.json`` (git-ignored runtime state) is
  written only at mutation endpoints (node release / registry reset), never
  on the per-write hot path, and exists for crash diagnostics - a fresh
  compile always resets the registry because no task is in flight.
- ``FileClaimGate`` is the per-agent enforcement helper. It loads the set of
  git-tracked paths of the agent's own workspace root once (a single
  ``git ls-files`` snapshot; the tracked set cannot change while an agent
  runs, since its own writes stay uncommitted until the phase integrates).
  Tracked files (template files, merged sibling work, the node's own
  committed phases) are never claimed - the shared-surface and
  additive-merge rules already govern them. Untracked paths are claimed for
  the current node; a path already claimed by a *sibling* node is rejected
  with a message that tells the model to pick a node-owned path instead.

Two roots with distinct semantics: the registry is keyed on the
*integration* workspace (``claims_workspace_root``, shared by all nodes),
while the tracked/untracked check runs against the *agent's* filesystem root
(the task worktree in parallel mode). A sibling's committed-but-unmerged
file is untracked in this worktree precisely because its branch is
invisible here - that is the arbitration the claims provide.

The registry is best-effort prevention (any git failure fails open: no
claims, no blocking); merge conflicts that still slip through are handled
by the workflow's conflict-aware DESIGN retry.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Callable

CLAIMS_RELPATH = (".arc", "file_claims.json")


class FileClaimRegistry:
    """Thread-safe map of workspace-relative path -> owning node id."""

    def __init__(self, workspace_path: str) -> None:
        self._root = Path(workspace_path).expanduser().resolve()
        self._claims: dict[str, str] = {}
        self._lock = threading.Lock()
        self._load()

    @property
    def workspace_path(self) -> str:
        return str(self._root)

    def _path(self) -> Path:
        return self._root.joinpath(*CLAIMS_RELPATH)

    def _load(self) -> None:
        path = self._path()
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt snapshot never blocks enforcement: the in-process
            # map is the source of truth and starts empty either way.
            return
        if isinstance(payload, dict) and all(
            isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
        ):
            self._claims = dict(payload)

    def _save_locked(self) -> None:
        """Snapshot the claims map to disk (called at mutation endpoints).

        Persistence is diagnostic only - a fresh compile resets the
        registry - so the write is deliberately kept off the per-write hot
        path and tolerates every filesystem failure silently.
        """

        if not self._root.is_dir():
            # Never fabricate the workspace root (tests construct registries
            # against placeholder roots); claims stay in-process only.
            return
        path = self._path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(self._claims, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(path)
        except OSError:
            pass

    def reset(self) -> None:
        """Drop every claim (fresh compile: nothing is in flight anymore)."""

        with self._lock:
            self._claims.clear()
            self._save_locked()

    def claim(self, rel_path: str, node_id: str) -> str | None:
        """Claim ``rel_path`` for ``node_id`` (in-process only).

        Returns the owning node id when a *different* node already claimed
        the path (the write must be blocked); ``None`` when this node may
        write, including when it already owns the claim. The on-disk
        snapshot is refreshed by ``release_node``/``reset``, not here.
        """

        if not rel_path or not node_id:
            return None
        with self._lock:
            owner = self._claims.get(rel_path)
            if owner is not None and owner != node_id:
                return owner
            if owner is None:
                self._claims[rel_path] = node_id
            return None

    def release_node(self, node_id: str) -> list[str]:
        """Release every claim owned by ``node_id``; returns the freed paths.

        Called when the node's task workspace closes: after a successful
        merge the files are tracked in git (claims are moot), and after a
        terminal failure the paths should be free for other nodes. This is
        also the point where the claim map is persisted to disk.
        """

        with self._lock:
            freed = [path for path, owner in self._claims.items() if owner == node_id]
            if not freed:
                return []
            self._claims = {
                path: owner for path, owner in self._claims.items() if owner != node_id
            }
            self._save_locked()
            return freed


def normalize_claim_path(virtual_path: str) -> str:
    """Reduce a tool-call path to a workspace-relative claim key.

    Accepts the virtual forms seen in file tool arguments (``/workspace/x``,
    ``/x`` produced from relative paths by the discipline layer) and plain
    relative paths; backslashes are normalised. Other virtual mounts
    (``/skills/...``) return ``""`` and are never claimed.
    """

    normalized = str(virtual_path or "").replace("\\", "/").strip()
    if not normalized:
        return ""
    if normalized == "/workspace" or normalized.startswith("/skills/") or normalized == "/skills":
        return ""
    if normalized.startswith("/workspace/"):
        normalized = normalized[len("/workspace/"):]
    return normalized.strip("/")


def _load_tracked_paths(agent_root: str) -> set[str] | None:
    """Snapshot the git-tracked paths of ``agent_root`` (repo-root relative).

    Returns ``None`` when git cannot run or the root is not a repository:
    the gate then fails open (every path treated as tracked, so no claim is
    made and nothing is blocked). Membership is checked in Python instead of
    per-path ``git ls-files --error-unmatch`` so glob-special path segments
    (e.g. Next.js dynamic routes ``[id]``) keep their literal meaning.
    """

    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=agent_root,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw: bytes = result.stdout or b""
    return {
        entry for entry in (chunk.decode("utf-8", "surrogateescape") for chunk in raw.split(b"\0")) if entry
    }


class FileClaimGate:
    """Per-agent write gate enforcing the claim registry for new files."""

    def __init__(
        self,
        registry: FileClaimRegistry,
        *,
        node_id: str,
        agent_root: str,
        tracked_loader: Callable[[str], set[str] | None] | None = None,
    ) -> None:
        self._registry = registry
        self._node_id = node_id
        self._agent_root = str(Path(agent_root).expanduser().resolve())
        self._tracked_loader = tracked_loader or _load_tracked_paths
        self._tracked: set[str] | None | None = None
        self._tracked_loaded = False
        self._lock = threading.Lock()

    def _tracked_paths(self) -> set[str] | None:
        if not self._tracked_loaded:
            # One snapshot per agent: the tracked set cannot change while the
            # agent runs (its own writes stay uncommitted until integrate).
            # The mid-phase replay (issue #127) is the sanctioned exception -
            # it lands a sibling's tracked files mid-run - and drops the
            # snapshot through ``invalidate_tracked_snapshot``.
            self._tracked = self._tracked_loader(self._agent_root)
            self._tracked_loaded = True
        return self._tracked

    def invalidate_tracked_snapshot(self) -> None:
        """Drop the tracked-set snapshot so the next check reloads it.

        Called after the mid-phase replay moved the worktree onto a sibling's
        merged tree: files that were untracked here (sibling-owned, its
        branch invisible) are now tracked, and the claim arbitration must
        see that instead of the stale pre-replay set.
        """

        with self._lock:
            self._tracked = None
            self._tracked_loaded = False

    def check_and_claim(self, virtual_path: str) -> str | None:
        """Validate a write to ``virtual_path`` and claim new-file paths.

        Returns a blocking message when a sibling node already claimed the
        path; ``None`` when the write may proceed.
        """

        rel_path = normalize_claim_path(virtual_path)
        if not rel_path:
            return None
        tracked = self._tracked_paths()
        if tracked is None:
            # Git unavailable: fail open, never block on tooling state.
            return None
        if rel_path in tracked:
            # Tracked in git: a template file, shared surface, or a sibling's
            # already-merged work. Ownership of those is governed by the
            # stage prompts and the additive merge resolver, not by claims.
            return None
        owner = self._registry.claim(rel_path, self._node_id)
        if owner is not None:
            return (
                f"Write blocked: {rel_path} was created by parallel node {owner}, which "
                "owns this new file in its isolated worktree; writing it here would "
                "produce an add/add merge conflict that fails this node. Use a different "
                "node-owned file path (name the file after this requirement's own "
                "domain) and wire it from the shared surfaces, or record the contract "
                "in the stage response for the file's owning node."
            )
        return None


_registries: dict[str, FileClaimRegistry] = {}
_registries_lock = threading.Lock()


def get_file_claim_registry(workspace_path: str) -> FileClaimRegistry:
    """Process-wide registry for the workspace (one per integration root)."""

    key = str(Path(workspace_path).expanduser().resolve())
    with _registries_lock:
        registry = _registries.get(key)
        if registry is None:
            registry = FileClaimRegistry(key)
            _registries[key] = registry
        return registry
