"""Small additive continuation tool for the DESIGN stage.

DESIGN materializes one compact, shape-only skeleton per file in a single
``write_file`` (issue #158 / ADR 0005: a skeleton that does not fit one
compact write is not a skeleton — the behavior description goes into the
stage response for TestDrivenDeveloper, never into chunks). ``append_file``
exists for the two cases that remain after that rule:

- additive wiring into the template's shared runtime surfaces (app entry,
  server entry, database lifecycle, shared api client), whose whole-file
  ``write_file`` is rejected by the shared-surface guard; and
- a small legitimate addition (a route row, a table declaration) to a file
  this stage already wrote, since a second ``write_file``/``edit_file`` on
  a written path is locked.

Enforcement split: ``StageDisciplineMiddleware`` owns the ownership policy
(claims, write budget, per-pass append budget, content sniffing — it sees
the same raw call), while this tool owns the filesystem reality: existence,
the agent's write-permission rules, and the per-call line backstop it
reports on.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from agents.runtime.stage_discipline import (
    MAX_APPEND_LINES,
    MAX_APPENDS_PER_FILE,
    MAX_EDITS_PER_PATH,
    append_line_limit_message,
)
from core.file_claims import normalize_claim_path

_LOGGER = logging.getLogger(__name__)

APPEND_FILE_TOOL_DESCRIPTION = f"""Append a few lines to the end of an existing file, without re-emitting the file.

Usage:
- Use this tool only for small additive continuations: wiring your node-owned module into the template's shared runtime surfaces (whose whole-file `write_file` is rejected), or adding a small piece such as a route row or a table declaration to a file you already wrote this stage. A file you have already written cannot be rewritten wholesale (`write_file` on it is blocked; `edit_file` takes at most {MAX_EDITS_PER_PATH} refinements before it blocks too), so appending is the sanctioned way to keep extending it.
- Appends are strictly additive: existing lines are never modified, and the tool is capped at {MAX_APPEND_LINES} lines per call and {MAX_APPENDS_PER_FILE} appends per file.
- A skeleton that does not fit in one compact `write_file` is not a skeleton: do not split it into chunks or use appends as continuations; put the complete behavior description in your stage response for TestDrivenDeveloper. DESIGN still only materializes skeletons.
- `file_path` is a `/workspace/...` path or a workspace-relative path; the file must already exist.
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
        """Append a few lines to the end of an existing file."""

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
                f"Error: {virtual_path} does not exist; `append_file` extends an existing file - "
                "materialize the file first with write_file."
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
            return f"Error: {append_line_limit_message(appended_lines)}"

        try:
            with path.open("a", encoding="utf-8") as handle:
                if needs_separator:
                    handle.write("\n")
                handle.write(body)
        except OSError as exc:
            return f"Error: could not append to {virtual_path} ({exc.__class__.__name__}); nothing was written."

        total_lines = existing_lines + appended_lines
        return (
            f"Appended {appended_lines} line(s) to {virtual_path}; the file is now {total_lines} line(s)."
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
