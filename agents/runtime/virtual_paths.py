"""Virtual ``/workspace`` path normalization at the tool boundary.

Models sometimes address a generated app file as ``/frontend/...`` or
``/backend/...`` even though the filesystem tools expose the project at
``/workspace``. This module keeps that repair narrow and auditable:

* known project roots (``frontend`` and ``backend``) may gain the missing
  ``/workspace`` prefix;
* host absolute paths, traversal paths, and other virtual roots are never
  rewritten;
* every path-bearing call exposes its request, classification, and execution
  path to the tool-usage observer through a context variable.

The middleware runs before stage discipline and the filesystem middleware, so
all downstream policy checks see the same path that the filesystem tool will
execute. The original request remains available for runner-event auditing.
"""

from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from deepagents.backends.utils import validate_path
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ToolCallRequest


# Built-in filesystem tools and ARC's additive file tool use ``file_path``;
# directory/search tools use ``path``. A single map keeps the normalization
# boundary explicit and prevents unrelated tool arguments from being
# reinterpreted as filesystem paths.
PATH_ARGUMENTS: dict[str, str] = {
    "read_file": "file_path",
    "write_file": "file_path",
    "edit_file": "file_path",
    "delete": "file_path",
    "append_file": "file_path",
    "ls": "path",
    "glob": "path",
    "grep": "path",
}

PROJECT_ROOTS = frozenset({"backend", "frontend"})
PROJECT_ROOT_CANDIDATES = frozenset({"app", "backend", "frontend", "tests"})

PATH_CLASS_WORKSPACE = "workspace"
PATH_CLASS_VIRTUAL_ROOT = "virtual_root"
PATH_CLASS_MISSING_PREFIX = "missing_workspace_prefix"
PATH_CLASS_HOST_ABSOLUTE = "host_absolute"
PATH_CLASS_TRAVERSAL = "traversal"
PATH_CLASS_OTHER_WORKSPACE = "other_workspace"
PATH_CLASS_UNKNOWN_ROOT = "unknown_virtual_root"
PATH_CLASS_MISSING = "missing"
PATH_CLASS_EMPTY = "empty"
PATH_CLASS_INVALID_ARGUMENT = "invalid_argument"
PATH_CLASS_INVALID = "invalid"

_REJECTED_CLASSES = frozenset(
    {
        PATH_CLASS_HOST_ABSOLUTE,
        PATH_CLASS_TRAVERSAL,
        PATH_CLASS_OTHER_WORKSPACE,
        PATH_CLASS_UNKNOWN_ROOT,
        PATH_CLASS_INVALID_ARGUMENT,
        PATH_CLASS_INVALID,
    }
)


@dataclass(frozen=True)
class VirtualPathAudit:
    """Audit data for one path-bearing tool call."""

    tool: str
    tool_call_id: str
    argument: str
    requested_path: str | None
    classification: str
    execution_path: str | None
    normalized: bool = False


_current_audit: contextvars.ContextVar[VirtualPathAudit | None] = contextvars.ContextVar(
    "arc_current_virtual_path_audit",
    default=None,
)


def current_virtual_path_audit(tool_call_id: str | None = None) -> VirtualPathAudit | None:
    """Return the audit for the currently executing tool call, if any."""

    audit = _current_audit.get()
    if audit is None or tool_call_id is None:
        return audit
    return audit if audit.tool_call_id == str(tool_call_id) else None


def _is_host_absolute(path: str) -> bool:
    """Whether ``path`` names a host filesystem location."""

    # ``validate_path`` rejects drive-letter paths, while the leading double
    # slash catches UNC and extended-length forms after slash normalization.
    return bool(re.match(r"^[A-Za-z]:", path)) or path.startswith("//")


def _has_traversal(path: str) -> bool:
    return ".." in path.split("/") or path == "~" or path.startswith("~/")


def project_roots_for_workspace(workspace_root: str | Path | None) -> frozenset[str]:
    """Return safe known project roots present in one generated workspace."""

    roots = set(PROJECT_ROOTS)
    if workspace_root:
        root = Path(workspace_root).expanduser()
        for candidate in PROJECT_ROOT_CANDIDATES:
            try:
                if (root / candidate).is_dir():
                    roots.add(candidate)
            except OSError:
                continue
    return frozenset(roots)


def classify_virtual_path(
    value: object,
    *,
    project_roots: Iterable[str] = PROJECT_ROOTS,
) -> tuple[str, str | None, bool]:
    """Return ``(classification, execution_path, normalized)`` for a path.

    ``execution_path`` is the canonical virtual path that downstream tools
    would receive. It is ``None`` when the input cannot safely be sent to a
    filesystem tool. Only the two known app roots are eligible for prefix
    insertion; every other non-``/workspace`` root stays untouched.
    """

    if value is None:
        return PATH_CLASS_MISSING, None, False
    if not isinstance(value, str):
        return PATH_CLASS_INVALID_ARGUMENT, None, False

    raw = value.strip()
    if not raw:
        return PATH_CLASS_EMPTY, None, False

    slash_path = raw.replace("\\", "/")
    if _is_host_absolute(slash_path):
        return PATH_CLASS_HOST_ABSOLUTE, None, False
    if _has_traversal(slash_path):
        return PATH_CLASS_TRAVERSAL, None, False

    try:
        canonical = validate_path(raw)
    except ValueError:
        return PATH_CLASS_INVALID, None, False

    if canonical == "/workspace" or canonical.startswith("/workspace/"):
        return PATH_CLASS_WORKSPACE, canonical, canonical != raw
    if canonical == "/skills" or canonical.startswith("/skills/"):
        return PATH_CLASS_VIRTUAL_ROOT, canonical, canonical != raw

    first_segment = canonical.removeprefix("/").split("/", 1)[0]
    if first_segment in project_roots:
        return PATH_CLASS_MISSING_PREFIX, f"/workspace{canonical}", True

    # A path that looks like a different workspace must remain distinguishable
    # from a project-relative path. In particular, never turn
    # ``/workspace-2/...`` into a path inside the current workspace.
    if canonical.startswith("/workspace"):
        return PATH_CLASS_OTHER_WORKSPACE, canonical, False

    return PATH_CLASS_UNKNOWN_ROOT, canonical, False


def prepare_virtual_path_request(
    request: "ToolCallRequest",
    *,
    project_roots: Iterable[str] = PROJECT_ROOTS,
) -> tuple["ToolCallRequest", VirtualPathAudit | None]:
    """Normalize one tool request and return its audit record."""

    call = request.tool_call
    tool = str(call.get("name") or "")
    argument = PATH_ARGUMENTS.get(tool)
    args = call.get("args")
    if argument is None or not isinstance(args, dict):
        return request, None

    requested = args.get(argument)
    classification, execution_path, normalized = classify_virtual_path(
        requested,
        project_roots=project_roots,
    )
    audit = VirtualPathAudit(
        tool=tool,
        tool_call_id=str(call.get("id") or ""),
        argument=argument,
        requested_path=requested if isinstance(requested, str) else None,
        classification=classification,
        execution_path=execution_path,
        normalized=normalized,
    )

    if execution_path is None or not isinstance(requested, str) or execution_path == requested:
        return request, audit
    return request.override(tool_call={**call, "args": {**args, argument: execution_path}}), audit


def _diagnostic(audit: VirtualPathAudit) -> str:
    requested = audit.requested_path if audit.requested_path is not None else "<missing>"
    return (
        "[ARC path diagnostic]\n"
        f"requested_path: {requested}\n"
        f"classification: {audit.classification}\n"
        "execution_path: <rejected>\n"
        "action: use /workspace/<path> for generated project files; host "
        "absolute paths, traversal paths, and other workspace roots are rejected."
    )


def _annotate_rejection(result: Any, audit: VirtualPathAudit) -> Any:
    """Add a structured rejection hint without changing success payloads."""

    if audit.classification not in _REJECTED_CLASSES:
        return result

    if isinstance(result, ToolMessage):
        if result.status != "error" or not isinstance(result.content, str):
            return result
        result.content = f"{result.content}\n{_diagnostic(audit)}"
        return result

    # ``append_file`` is a custom tool that returns plain text rather than a
    # ToolMessage. Keep its existing error wording and add the same audit hint.
    if isinstance(result, str) and result.startswith("Error:"):
        return f"{result}\n{_diagnostic(audit)}"
    return result


class VirtualWorkspacePathMiddleware(AgentMiddleware[Any, Any, Any]):
    """Normalize known missing ``/workspace`` prefixes before tool execution."""

    def __init__(self, *, project_roots: Iterable[str] | None = None) -> None:
        self._project_roots = frozenset(project_roots) if project_roots is not None else PROJECT_ROOTS

    def wrap_tool_call(
        self,
        request: "ToolCallRequest",
        handler: "Callable[[ToolCallRequest], Any]",
    ) -> Any:
        prepared, audit = prepare_virtual_path_request(request, project_roots=self._project_roots)
        if audit is None:
            return handler(prepared)
        token = _current_audit.set(audit)
        try:
            return _annotate_rejection(handler(prepared), audit)
        finally:
            _current_audit.reset(token)

    async def awrap_tool_call(
        self,
        request: "ToolCallRequest",
        handler: "Callable[[ToolCallRequest], Awaitable[Any]]",
    ) -> Any:
        prepared, audit = prepare_virtual_path_request(request, project_roots=self._project_roots)
        if audit is None:
            return await handler(prepared)
        token = _current_audit.set(audit)
        try:
            return _annotate_rejection(await handler(prepared), audit)
        finally:
            _current_audit.reset(token)
