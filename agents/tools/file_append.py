"""Append-only continuation of a file, so DESIGN can build skeletons in pieces.

InterfaceDesigner materializes interface skeletons, and the stage's write rules
block a second ``write_file``/``edit_file`` on a path it already wrote
(repeated writes are the stage's main self-review loop). Without an additive
tool the only way to grow a file is to regenerate all of it in one model turn:
run8 (2026-09-17) shows the cost — the model was blocked twice at the 160-line
skeleton cap before it compressed ``RegisterPage.tsx`` into one write, and the
slowest DESIGN rounds on the ticket-booking benchmark are exactly those
whole-file generations (153-358 s per turn).

``append_file`` gives the stage a sanctioned continuation: the model opens a
file with a small ``write_file`` skeleton and then adds sections with
``append_file``, so a turn only emits its own chunk instead of the whole file.

Enforcement split (deliberately not duplicated):

- ``StageDisciplineMiddleware`` owns *ownership* policy. It treats
  ``append_file`` as a write for claims, bookkeeping and the ``materialized
  paths`` ground truth, keeps the repeated-write lock on ``write_file``/
  ``edit_file`` only, and enforces the per-path append budget.
- This tool owns *file-size* policy, because it is the only place that can read
  the current file: the per-file DESIGN skeleton ceiling is checked against the
  file's real line count, and the result reports the remaining budget so the
  model can plan the next chunk.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from agents.runtime.stage_discipline import MAX_APPEND_LINES, MAX_APPENDS_PER_FILE, MAX_SKELETON_LINES
from core.file_claims import normalize_claim_path

_LOGGER = logging.getLogger(__name__)

APPEND_FILE_TOOL_DESCRIPTION = f"""Append a chunk to the end of a file, without re-emitting the file.

Usage:
- Use this tool to grow a file you already started in this stage: `write_file` the skeleton first (imports, types, exported signatures, routes, table declarations, TODO boundaries), then `append_file` each remaining section. A file you already wrote cannot be rewritten (`write_file`/`edit_file` on it are blocked), so appending is the sanctioned way to extend it.
- Appends are strictly additive: existing lines are never modified, and the tool is capped at {MAX_APPEND_LINES} lines per call, {MAX_SKELETON_LINES} lines per file, and {MAX_APPENDS_PER_FILE} appends per file. Keep each chunk to one cohesive section.
- Do not append content that is already in the file, and do not use this tool to implement business behavior; DESIGN still only materializes skeletons.
- `file_path` is a `/workspace/...` path or a workspace-relative path; write the initial skeleton with `write_file` before appending.
"""


def build_append_file_tool(
    *,
    workspace_root: str,
    permissions: list[Any] | None = None,
) -> BaseTool:
    """Build the ``append_file`` tool for one agent workspace.

    ``permissions`` is the same permission list the agent's built-in file tools
    are checked against, so the append path cannot bypass the read/write
    policy (denied dependency directories, runtime state, lockfiles) that
    governs ``write_file``.
    """

    root = Path(workspace_root).expanduser().resolve()

    async def append_file(file_path: str, content: str) -> str:
        """Append a chunk of text to the end of an existing skeleton file."""

        target = _resolve_target(root, file_path)
        if isinstance(target, str):
            return target
        virtual_path, path = target

        denial = _permission_denial(permissions, virtual_path)
        if denial:
            return denial

        body = content if content.endswith("\n") else f"{content}\n"
        if not content.strip():
            return f"Error: `append_file` received empty content for {virtual_path}; nothing was written."

        if not path.exists():
            return (
                f"Error: {virtual_path} does not exist; write the initial DESIGN skeleton with write_file "
                "before using append_file."
            )
        existing_lines = 0
        needs_separator = False
        if path.is_file():
            try:
                existing = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return f"Error: could not read {virtual_path} before appending ({exc.__class__.__name__}); nothing was written."
            existing_lines = len(existing.splitlines())
            needs_separator = bool(existing) and not existing.endswith("\n")
        elif path.exists():
            return f"Error: {virtual_path} is not a file; `append_file` appends to files only."

        appended_lines = len(body.splitlines())
        if appended_lines > MAX_APPEND_LINES:
            return (
                f"Error: append_file accepts at most {MAX_APPEND_LINES} lines per chunk; received {appended_lines}. "
                "Split the next cohesive skeleton section into another append."
            )
        total_lines = existing_lines + appended_lines
        if total_lines > MAX_SKELETON_LINES:
            return (
                f"Error: appending {appended_lines} line(s) would put {virtual_path} at {total_lines} line(s), "
                f"over the {MAX_SKELETON_LINES}-line DESIGN skeleton ceiling. Record the remaining contract detail "
                "in your design response (for TestDrivenDeveloper) instead of extending this file, or move the "
                "behavior into one of your other skeleton files."
            )

        try:
            with path.open("a", encoding="utf-8") as handle:
                if needs_separator:
                    handle.write("\n")
                handle.write(body)
        except OSError as exc:
            return f"Error: could not append to {virtual_path} ({exc.__class__.__name__}); nothing was written."

        remaining = MAX_SKELETON_LINES - total_lines
        return (
            f"Appended {appended_lines} line(s) to {virtual_path}; the file is now {total_lines} line(s) "
            f"({remaining} line(s) of the {MAX_SKELETON_LINES}-line DESIGN skeleton budget left)."
        )

    return StructuredTool.from_function(
        coroutine=append_file,
        name="append_file",
        description=APPEND_FILE_TOOL_DESCRIPTION,
    )


def _resolve_target(root: Path, raw_path: str) -> tuple[str, Path] | str:
    """Resolve a tool-call path to (virtual path, absolute path) or an error."""

    rel_path = normalize_claim_path(raw_path)
    if not rel_path:
        return (
            "Error: `append_file` works on `/workspace` paths only; "
            f"{str(raw_path or '').strip() or '<empty>'} is outside the project root."
        )
    if ".." in rel_path.split("/"):
        return f"Error: `append_file` does not accept traversal segments (`..`) in {rel_path}."

    virtual_path = f"/workspace/{rel_path}"
    candidate = root / rel_path
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return f"Error: could not resolve {virtual_path} ({exc.__class__.__name__})."
    if resolved != root and root not in resolved.parents:
        return f"Error: {virtual_path} resolves outside the project root; nothing was written."
    return virtual_path, resolved


def _permission_denial(permissions: list[Any] | None, virtual_path: str) -> str | None:
    """Apply the agent's own write permission rules to the append target.

    The check reuses deepagents' first-match-wins resolver so the built-in file
    tools and this tool cannot disagree about what is writable. A deepagents
    release that drops the helper fails closed (the append is refused, the
    message says so) rather than silently appending to a denied path.
    """

    if not permissions:
        return None
    try:
        from deepagents.middleware.filesystem import _check_fs_permission
    except Exception:
        _LOGGER.warning(
            "deepagents no longer exposes the filesystem permission resolver; refusing `append_file` writes"
        )
        return (
            f"Error: `append_file` cannot verify the write policy for {virtual_path} in this runtime; "
            "use `write_file`/`edit_file` instead."
        )
    try:
        decision = _check_fs_permission(permissions, "write", virtual_path)
    except Exception:
        _LOGGER.warning("filesystem permission resolution failed for %s; refusing the append", virtual_path)
        return (
            f"Error: `append_file` could not resolve the write policy for {virtual_path}; "
            "use `write_file`/`edit_file` instead."
        )
    if decision != "allow":
        return f"Error: permission denied for write on {virtual_path}"
    return None
