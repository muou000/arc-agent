"""Cross-node ownership registry for new files created in parallel worktrees.

Sibling requirement nodes compile in isolated git worktrees branched from the
same integration HEAD, so a file created by one node is invisible to the
others until its branch merges back. When two nodes both create the same
path, the second merge hits an add/add conflict that the additive resolver
explicitly refuses (a new file has no common base to append to) and the whole
node fails. This module prevents that class of failure at write time:

- ``FileClaimRegistry`` maps workspace-relative paths to the node id that
  first created them as a *new* (git-untracked) file. It is shared by every
  in-flight task in the process and persisted under
  ``<workspace>/.arc/file_claims.json`` (git-ignored runtime state) so a
  resumed process can rebuild it.
- ``FileClaimGate`` is the per-agent enforcement helper: before a
  ``write_file``/``edit_file`` lands, the gate asks git whether the path is
  tracked. Tracked files (template files, merged sibling work, the node's
  own committed phases) are never claimed - the shared-surface and
  additive-merge rules already govern them. Untracked paths are claimed for
  the current node; a path already claimed by a *sibling* node is rejected
  with a message that tells the model to pick a node-owned path instead.

The registry is best-effort prevention; merge conflicts that still slip
through are handled by the workflow's conflict-aware DESIGN retry.
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
            return
        if isinstance(payload, dict) and all(
            isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
        ):
            self._claims = dict(payload)

    def _save_locked(self) -> None:
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
            # Persistence is a resume nicety, not a correctness requirement;
            # the in-process map keeps enforcing claims either way.
            pass

    def reset(self) -> None:
        """Drop every claim (fresh compile: nothing is in flight anymore)."""

        with self._lock:
            self._claims.clear()
            self._save_locked()

    def claim(self, rel_path: str, node_id: str) -> str | None:
        """Claim ``rel_path`` for ``node_id``.

        Returns the owning node id when a *different* node already claimed
        the path (the write must be blocked); ``None`` when this node may
        write, including when it already owns the claim.
        """

        if not rel_path or not node_id:
            return None
        with self._lock:
            owner = self._claims.get(rel_path)
            if owner is not None and owner != node_id:
                return owner
            if owner is None:
                self._claims[rel_path] = node_id
                self._save_locked()
            return None

    def release_node(self, node_id: str) -> list[str]:
        """Release every claim owned by ``node_id``; returns the freed paths.

        Called when the node's task workspace closes: after a successful
        merge the files are tracked in git (claims are moot), and after a
        terminal failure the paths should be free for other nodes.
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
    if normalized.startswith("/workspace/"):
        normalized = normalized[len("/workspace/"):]
    elif normalized == "/workspace":
        return ""
    elif normalized.startswith("/skills/") or normalized == "/skills":
        return ""
    normalized = normalized.strip("/")
    return normalized


def _git_tracked(agent_root: str, rel_path: str) -> bool:
    """Whether ``rel_path`` is tracked by git in the agent's workspace root.

    Fails open (treated as tracked, so no claim is made) when git cannot
    run or the root is not a git repository: claiming only has meaning for
    worktree-parallel compilation inside a real repo, and it must never
    block workspaces that do not use git (tests, plain directories).
    """

    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", rel_path],
            cwd=agent_root,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    stderr = result.stderr or b""
    if result.returncode != 0 and b"not a git repository" in stderr.lower():
        return True
    return result.returncode == 0


class FileClaimGate:
    """Per-agent write gate enforcing the claim registry for new files."""

    def __init__(
        self,
        registry: FileClaimRegistry,
        *,
        node_id: str,
        agent_root: str,
        tracked_check: Callable[[str, str], bool] | None = None,
    ) -> None:
        self._registry = registry
        self._node_id = node_id
        self._agent_root = str(Path(agent_root).expanduser().resolve())
        self._tracked_check = tracked_check or _git_tracked
        self._tracked_cache: dict[str, bool] = {}
        self._cache_lock = threading.Lock()

    def check_and_claim(self, virtual_path: str) -> str | None:
        """Validate a write to ``virtual_path`` and claim new-file paths.

        Returns a blocking message when a sibling node already claimed the
        path; ``None`` when the write may proceed. Only paths untracked in
        this agent's workspace root are claimable; the git-tracked status of
        a path cannot change during one agent run, so it is cached.
        """

        rel_path = normalize_claim_path(virtual_path)
        if not rel_path:
            return None
        with self._cache_lock:
            tracked = self._tracked_cache.get(rel_path)
        if tracked is None:
            tracked = self._tracked_check(self._agent_root, rel_path)
            with self._cache_lock:
                self._tracked_cache[rel_path] = tracked
        if tracked:
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
