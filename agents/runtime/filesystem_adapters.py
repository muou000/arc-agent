"""Build-time filesystem adapters for the stage agents.

Four delivered behaviors used to be installed by rewriting imported library
classes at process level (module-global idempotency sentinels plus class and
module attribute swaps inside ``build_stage_agent``). They now live on seams
deepagents already exposes and are constructed once per agent build, so
production and tests instantiate the same objects:

- ``ARCFilesystemMiddleware`` replaces the stock ``FilesystemMiddleware``
  through ``create_deep_agent``'s by-name custom middleware replacement: a
  custom middleware whose ``.name`` matches a core stack entry is swapped in
  place, preserving stack order. Its delete tool reports ``not found`` for
  confirmed-missing targets instead of deny-rule spam.
- ``PermissionDeniedHintMiddleware`` is an ordinary middleware appended to the
  stack; it rewrites permission-denied tool results with the valid virtual
  roots.
- ``WindowsCompatFilesystemBackend`` is a plain ``FilesystemBackend`` subclass
  selected by ``workspace_filesystem_backend`` on Windows so extended-length
  ``\\\\?\\`` path forms keep passing the virtual containment checks.

The fourth delivered behavior — read_file output without line-number padding —
needs no adapter on deepagents >= 0.7: the upstream read_file body is emitted
verbatim (``_format_source_block``). The former process-level patch rewrote a
formatter that no longer exists; tests pin the verbatim-output contract
through the build path instead (``tests/test_agents/test_permission_denied_hint.py``).

A fifth behavior, new rather than historical: the ``write_file``/``edit_file``
success receipts gain an integrity trailer (``bytes_written`` plus the first 8
hex chars of the content's sha256) built on the same upstream tool factory
seams as the delete tool, plus a standing note that the context echo's
``...(argument truncated)`` marker is display truncation only.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deepagents import __version__ as _deepagents_version
from deepagents.backends import FilesystemBackend
from deepagents.backends.utils import validate_path
from deepagents.middleware.filesystem import (
    DeleteSchema,
    EditFileSchema,
    FilesystemMiddleware,
    WriteFileSchema,
)
from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

# Unlike the delete deny-pattern helper below, this upstream import is
# deliberately unguarded: it backs a containment hardening check, so an
# upstream rename must fail the build loudly instead of silently dropping it.
from deepagents.backends.filesystem import _raise_if_symlink_loop as _symlink_loop_guard

from core.path_compat import (
    normalize_windows_extended_prefix_path,
    normalize_windows_extended_prefix_text,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ToolCallRequest
    from langchain_core.tools import BaseTool


# One-line remediation appended to permission-denied tool results. The raw
# upstream message names only the denied virtual path, so a model that used a
# host-style ("/frontend/src/...") or relative ("backend/x.py") path has to
# guess the valid root from the system prompt — observed online as repeated
# retries against the same denied path before the correction lands.
_PERMISSION_DENIED_PREFIX = "Error: permission denied for "
_PERMISSION_DENIED_HINT = (
    " (ARC virtual filesystem: address files as /workspace/<path> for the "
    "generated app and /skills/<name>/SKILL.md for attached skills; host or "
    "relative paths are not valid tool paths.)"
)


class PermissionDeniedHintMiddleware(AgentMiddleware[Any, Any, Any]):
    """Append the valid virtual roots to permission-denied tool results.

    The message keeps its upstream prefix — tests and log scanners match on
    "permission denied" — and only gains the remediation suffix. Denied paths
    stay denied; the hint names roots that already exist in the system
    prompt's tool policy, so it leaks nothing about protected files.
    """

    def wrap_tool_call(
        self,
        request: "ToolCallRequest",
        handler: "Callable[[ToolCallRequest], Any]",
    ) -> Any:
        return self._with_hint(handler(request))

    async def awrap_tool_call(
        self,
        request: "ToolCallRequest",
        handler: "Callable[[ToolCallRequest], Awaitable[Any]]",
    ) -> Any:
        return self._with_hint(await handler(request))

    @staticmethod
    def _with_hint(tool_result: Any) -> Any:
        content = getattr(tool_result, "content", None)
        if isinstance(content, str) and content.startswith(_PERMISSION_DENIED_PREFIX):
            if _PERMISSION_DENIED_HINT not in content:
                tool_result.content = content + _PERMISSION_DENIED_HINT
        return tool_result


# The backends' explicit missing-path sentinel: both shipped producers format
# the ls error as exactly ``"Path '<path>': path_not_found"`` (FilesystemBackend
# directly, SandboxBackend via its JSON error passthrough), so anchor the match
# to that suffix. A bare substring check would also fire when the *path itself*
# contains the token (e.g. deleting a ``/workspace/path_not_found`` directory
# whose ls fails some other way) and wrongly relax the descendant check.
_NOT_FOUND_SUFFIX = ": path_not_found"


def _missing_by_ls_error(ls_result: Any) -> bool:
    """Whether an ls result is the backends' explicit ``path_not_found``."""

    error = getattr(ls_result, "error", None)
    return error is not None and str(error).endswith(_NOT_FOUND_SUFFIX)


def _confirmed_missing(backend: Any, target: str) -> bool:
    """Whether ``backend.ls(target)`` explicitly reports ``path_not_found``.

    Any probe failure (unsupported ``ls``, transient I/O error) counts as
    "not confirmed": the caller keeps upstream's conservative answer instead
    of letting the exception escape into the delete tool.
    """

    try:
        ls_result = backend.ls(target)
    except Exception:
        return False
    return _missing_by_ls_error(ls_result)


async def _aconfirmed_missing(backend: Any, target: str) -> bool:
    try:
        ls_result = await backend.als(target)
    except Exception:
        return False
    return _missing_by_ls_error(ls_result)


def _delete_deny_pattern_resolver() -> "Callable[..., list[str]] | None":
    """Resolve upstream's delete deny-pattern helper, or ``None`` if renamed.

    This is an upstream implementation detail, not public contract. A
    deepagents upgrade that renames or removes it must degrade to the old
    (spammier but harmless) delete behavior, not crash ``build_stage_agent``.
    """

    import deepagents.middleware.filesystem as filesystem_middleware

    helper = getattr(filesystem_middleware, "_find_delete_deny_patterns", None)
    if not callable(helper):
        logging.getLogger(__name__).warning(
            "deepagents %s no longer exposes the delete deny-pattern helper; "
            "keeping upstream delete behavior",
            _deepagents_version,
        )
        return None
    return helper


# Ground truth appended to successful write_file/edit_file receipts. Models
# misread the context echo's `...(argument truncated)` marker as "my arguments
# were cut off, the file now holds a truncated placeholder" (easy-ticketbooking
# 2026-09-21: TestGenerator burned ~11 minutes on a delete-rewrite loop plus
# budget workarounds triggered by that misread). The trailer lets the model
# mechanically self-verify what landed without re-reading the file; the note
# kills the misread at the first step. Upstream receipt lines are kept verbatim
# as the first line — log scanners and tests match on them.
_WRITE_RECEIPT_NOTE = (
    "Note: `...(argument truncated)` in the context echo is display truncation "
    "of long tool arguments only; it does not affect the actual written content."
)

# `read()` is a line-window API with no "everything" sentinel, so the edit
# receipt's read-back asks for every line with this cap. Only used against the
# just-edited file, never surfaced to the model as a read.
_WHOLE_FILE_READ_LIMIT = 2**31 - 1


def _integrity_lines(content: str) -> "list[str]":
    """``bytes_written``/``sha256`` receipt lines describing ``content``.

    Upstream persists text with ``encoding="utf-8", newline=""`` on both the
    write and edit paths, so utf-8 bytes are the exact disk truth; the edit
    receipt re-reads the file instead of trusting a recomposition.
    """

    encoded = content.encode("utf-8")
    return [
        f"bytes_written: {len(encoded)}",
        f"sha256: {hashlib.sha256(encoded).hexdigest()[:8]}",
    ]


def _success_text(tool_result: Any) -> "str | None":
    """The receipt text of a successful ToolMessage, else ``None``.

    Only string-content success receipts are trailer-eligible; anything else
    (errors, non-text payloads) passes through untouched.
    """

    if isinstance(tool_result, ToolMessage) and tool_result.status == "success":
        content = tool_result.content
        if isinstance(content, str):
            return content
    return None


def _with_receipt_trailer(tool_result: Any, lines: "list[str]") -> Any:
    """Append trailer lines to a *successful* receipt; pass everything else.

    Error receipts keep upstream's exact text — failure diagnosis and log
    scanners match on it, and a successful write must never be reshaped into
    (or accompanied by) an error-shaped message.
    """

    content = _success_text(tool_result)
    if content is None or not lines:
        return tool_result
    tool_result.content = content + "\n" + "\n".join(lines)
    return tool_result


class ARCFilesystemMiddleware(FilesystemMiddleware):
    """Stock filesystem middleware with ARC's delete not-found precedence.

    ``create_deep_agent`` replaces a core middleware when a custom middleware
    carries the same ``.name``, so this instance swaps in for the stock one in
    place; the agent stack keeps its documented shape and no upstream class or
    module attribute is rewritten.

    The delete tool is rebuilt on the upstream tool factory seam
    (``_create_delete_tool``) with one behavioral change. Upstream's delete
    decides *before* the permission check whether the target "may have
    descendants" (a recursive delete must scan every deny rule for subtree
    overlap). ``ls`` answering ``path_not_found`` is not on its leaf
    whitelist, so a missing path is treated as a possibly-populated directory:
    every ``**`` deny pattern (``/**``, ``/workspace/**/node_modules``, ...)
    overlaps, and the model is told "permission denied" with the full deny
    rule list — for a file that does not exist. Observed on the 12306
    benchmark as 3-4 retries against the same missing path.

    The adapter recognizes the backend's explicit ``path_not_found`` answer as
    "nothing to protect": permission resolution then follows the ordinary
    first-matching-rule path (same as ``write_file``). A missing path inside a
    writable root reaches the backend, whose own ``delete`` reports the honest
    ``not found``; a denied path is still refused before the backend runs
    (first matching deny rule), so delete cannot be used to probe which
    protected files exist. Any other ``ls`` outcome keeps upstream's
    conservative descendant check.

    The write_file and edit_file tools are likewise rebuilt on their upstream
    factory seams (``_create_write_file_tool``/``_create_edit_file_tool``),
    keeping upstream's validation, permission and backend behavior and only
    appending an integrity trailer to *successful* receipts: the model that
    just wrote sees ``bytes_written`` and a short sha256 of the content
    actually on disk, plus the standing note that the context echo's
    ``...(argument truncated)`` marker is display truncation only — without
    which a long write's echo truncation reads as "the file holds a truncated
    placeholder" and triggers delete-rewrite loops.
    """

    # Matches the stock core-stack entry so ``create_deep_agent`` replaces it
    # in place instead of appending a second filesystem middleware.
    name = "FilesystemMiddleware"

    def _create_delete_tool(self) -> "BaseTool":
        upstream_delete = super()._create_delete_tool()
        resolve_deny_patterns = _delete_deny_pattern_resolver()
        if resolve_deny_patterns is None:
            return upstream_delete
        return StructuredTool.from_function(
            name="delete",
            description=upstream_delete.description,
            func=self._delete_with_missing_precedence(upstream_delete, resolve_deny_patterns),
            coroutine=self._adelete_with_missing_precedence(upstream_delete, resolve_deny_patterns),
            infer_schema=False,
            args_schema=DeleteSchema,
        )

    def _delete_with_missing_precedence(
        self,
        upstream_delete: "BaseTool",
        resolve_deny_patterns: "Callable[..., list[str]]",
    ) -> "Callable[..., ToolMessage]":
        def sync_delete(file_path: str, runtime: ToolRuntime) -> ToolMessage:
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return self._path_error_message(e, runtime.tool_call_id)
            if not _confirmed_missing(self.backend, validated_path):
                return upstream_delete.func(file_path=file_path, runtime=runtime)
            return self._deny_or_delete(validated_path, runtime.tool_call_id, resolve_deny_patterns)

        return sync_delete

    def _adelete_with_missing_precedence(
        self,
        upstream_delete: "BaseTool",
        resolve_deny_patterns: "Callable[..., list[str]]",
    ) -> "Callable[..., Any]":
        async def async_delete(file_path: str, runtime: ToolRuntime) -> ToolMessage:
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return self._path_error_message(e, runtime.tool_call_id)
            if not await _aconfirmed_missing(self.backend, validated_path):
                return await upstream_delete.coroutine(file_path=file_path, runtime=runtime)
            return await self._adeny_or_delete(validated_path, runtime.tool_call_id, resolve_deny_patterns)

        return async_delete

    @staticmethod
    def _path_error_message(exc: ValueError, tool_call_id: Any) -> ToolMessage:
        return ToolMessage(
            content=f"Error: {exc}",
            name="delete",
            tool_call_id=tool_call_id,
            status="error",
        )

    # The deny message replicates upstream's delete refusal format verbatim
    # (deepagents.middleware.filesystem, sync_delete/async_delete inside
    # _create_delete_tool); log scanners and tests match on this wording.
    @staticmethod
    def _deny_message(validated_path: str, denying_patterns: "list[str]", tool_call_id: Any) -> ToolMessage:
        return ToolMessage(
            content=(
                f"Error: permission denied for write on {validated_path} "
                f"(matches deny rule(s): {', '.join(denying_patterns)})"
            ),
            name="delete",
            tool_call_id=tool_call_id,
            status="error",
        )

    @staticmethod
    def _delete_outcome_message(res: Any, tool_call_id: Any) -> ToolMessage:
        if res.error:
            return ToolMessage(
                content=res.error,
                name="delete",
                tool_call_id=tool_call_id,
                status="error",
            )
        return ToolMessage(
            content=f"Deleted {res.path}",
            name="delete",
            tool_call_id=tool_call_id,
            status="success",
        )

    def _deny_or_delete(
        self,
        validated_path: str,
        tool_call_id: Any,
        resolve_deny_patterns: "Callable[..., list[str]]",
    ) -> ToolMessage:
        """Leaf-mode delete of a confirmed-missing target: deny rules, then delete."""

        denying_patterns = resolve_deny_patterns(self._permissions, validated_path, has_descendants=False)
        if denying_patterns:
            return self._deny_message(validated_path, denying_patterns, tool_call_id)
        return self._delete_outcome_message(self.backend.delete(validated_path), tool_call_id)

    async def _adeny_or_delete(
        self,
        validated_path: str,
        tool_call_id: Any,
        resolve_deny_patterns: "Callable[..., list[str]]",
    ) -> ToolMessage:
        denying_patterns = resolve_deny_patterns(self._permissions, validated_path, has_descendants=False)
        if denying_patterns:
            return self._deny_message(validated_path, denying_patterns, tool_call_id)
        return self._delete_outcome_message(await self.backend.adelete(validated_path), tool_call_id)

    # ------------------------------------------------------------------
    # write_file / edit_file receipts with an integrity trailer.
    # The upstream tools keep doing the work; these wrappers only append
    # ground-truth lines to the success receipt (see _WRITE_RECEIPT_NOTE).
    # ------------------------------------------------------------------

    def _create_write_file_tool(self) -> "BaseTool":
        upstream_write = super()._create_write_file_tool()
        return StructuredTool.from_function(
            name="write_file",
            description=upstream_write.description,
            func=self._write_with_integrity_trailer(upstream_write.func),
            coroutine=self._awrite_with_integrity_trailer(upstream_write.coroutine),
            infer_schema=False,
            args_schema=WriteFileSchema,
        )

    def _write_with_integrity_trailer(
        self,
        upstream_write: "Callable[..., Any]",
    ) -> "Callable[..., ToolMessage]":
        def sync_write(file_path: str, content: str, runtime: ToolRuntime) -> ToolMessage:
            result = upstream_write(file_path=file_path, content=content, runtime=runtime)
            return _with_receipt_trailer(result, [*_integrity_lines(content), _WRITE_RECEIPT_NOTE])

        return sync_write

    def _awrite_with_integrity_trailer(
        self,
        upstream_write: "Callable[..., Any]",
    ) -> "Callable[..., Any]":
        async def async_write(file_path: str, content: str, runtime: ToolRuntime) -> ToolMessage:
            result = await upstream_write(file_path=file_path, content=content, runtime=runtime)
            return _with_receipt_trailer(result, [*_integrity_lines(content), _WRITE_RECEIPT_NOTE])

        return async_write

    def _create_edit_file_tool(self) -> "BaseTool":
        upstream_edit = super()._create_edit_file_tool()
        return StructuredTool.from_function(
            name="edit_file",
            description=upstream_edit.description,
            func=self._edit_with_integrity_trailer(upstream_edit.func),
            coroutine=self._aedit_with_integrity_trailer(upstream_edit.coroutine),
            infer_schema=False,
            args_schema=EditFileSchema,
        )

    def _edit_with_integrity_trailer(
        self,
        upstream_edit: "Callable[..., Any]",
    ) -> "Callable[..., ToolMessage]":
        def sync_edit(
            file_path: str,
            old_string: str,
            new_string: str,
            runtime: ToolRuntime,
            *,
            replace_all: bool = False,
        ) -> ToolMessage:
            result = upstream_edit(
                file_path=file_path,
                old_string=old_string,
                new_string=new_string,
                runtime=runtime,
                replace_all=replace_all,
            )
            return _with_receipt_trailer(result, self._edit_trailer_lines(result, self._read_back(file_path)))

        return sync_edit

    def _aedit_with_integrity_trailer(
        self,
        upstream_edit: "Callable[..., Any]",
    ) -> "Callable[..., Any]":
        async def async_edit(
            file_path: str,
            old_string: str,
            new_string: str,
            runtime: ToolRuntime,
            *,
            replace_all: bool = False,
        ) -> ToolMessage:
            result = await upstream_edit(
                file_path=file_path,
                old_string=old_string,
                new_string=new_string,
                runtime=runtime,
                replace_all=replace_all,
            )
            return _with_receipt_trailer(result, self._edit_trailer_lines(result, await self._aread_back(file_path)))

        return async_edit

    def _read_back(self, file_path: str) -> Any:
        """Whole-file read-back of a just-edited path, or ``None`` on any surprise."""

        try:
            return self.backend.read(validate_path(file_path), offset=0, limit=_WHOLE_FILE_READ_LIMIT)
        except Exception:
            return None

    async def _aread_back(self, file_path: str) -> Any:
        try:
            return await self.backend.aread(validate_path(file_path), offset=0, limit=_WHOLE_FILE_READ_LIMIT)
        except Exception:
            return None

    @staticmethod
    def _edit_trailer_lines(tool_result: Any, read_back: Any) -> "list[str]":
        """Integrity trailer for an edit receipt, hashed from the file on disk.

        Unlike write_file, the edited content is upstream's internal
        composition (its universal-newline read plus replacement), so the only
        truthful source is the file itself. After a successful edit the disk
        bytes are LF-only (upstream rewrites the whole file with
        ``newline=""``), so the line-window read-back is byte-faithful — pinned
        by test in ``tests/test_agents/test_write_receipt_integrity.py``.

        A windowed or non-text read-back (error, binary payload; empty or
        whitespace-only files carry no pagination metadata and a reminder
        string instead of content) degrades to the note-only trailer: a
        successful edit must never produce a missing or lying receipt.
        """

        if _success_text(tool_result) is None:
            return []
        if read_back is None:
            return [_WRITE_RECEIPT_NOTE]
        file_data = getattr(read_back, "file_data", None)
        body = file_data.get("content") if isinstance(file_data, dict) else None
        if (
            getattr(read_back, "error", None) is not None
            or not isinstance(body, str)
            or (isinstance(file_data, dict) and file_data.get("encoding") != "utf-8")
            or getattr(read_back, "total_lines", None) is None
        ):
            return [_WRITE_RECEIPT_NOTE]
        return [*_integrity_lines(body), _WRITE_RECEIPT_NOTE]


class WindowsCompatFilesystemBackend(FilesystemBackend):
    """``FilesystemBackend`` that survives Windows extended-length path forms.

    On Windows, ``Path.resolve()`` can surface ``\\\\?\\``-prefixed paths
    (extended-length form) for some or all of a workspace root and its
    children. Upstream's virtual containment check compares those raw string
    forms, so a resolved child in extended form no longer reports as inside a
    root in normal form — every read fails with "outside root directory".
    Normalizing both sides before the containment check keeps path comparisons
    stable; without an extended prefix the normalization is a no-op and the
    checks match upstream exactly.
    """

    def _resolve_path(self, key: str) -> Path:
        if not self.virtual_mode:
            return super()._resolve_path(key)

        raw_key = normalize_windows_extended_prefix_text(key)
        vpath = raw_key if raw_key.startswith("/") else "/" + raw_key
        if ".." in vpath or vpath.startswith("~"):
            raise ValueError("Path traversal not allowed")

        full = normalize_windows_extended_prefix_path((self.cwd / vpath.lstrip("/")).resolve())
        cwd = normalize_windows_extended_prefix_path(self.cwd)
        try:
            full.relative_to(cwd)
        except ValueError:
            msg = f"Path:{full} outside root directory: {cwd}"
            raise ValueError(msg) from None
        _symlink_loop_guard(full)
        return full

    def _to_virtual_path(self, path: Path) -> str:
        if not self.virtual_mode:
            return super()._to_virtual_path(path)

        full = normalize_windows_extended_prefix_path(path.resolve())
        cwd = normalize_windows_extended_prefix_path(self.cwd)
        return "/" + full.relative_to(cwd).as_posix()


def workspace_filesystem_backend(root_dir: str) -> FilesystemBackend:
    """The backend ``build_stage_agent`` routes ``/workspace/`` through.

    Windows gets the extended-length-path-compatible subclass; other
    platforms get the stock backend unchanged. Callers (production build and
    tests) go through this helper so both instantiate the same object.
    """

    if os.name == "nt":
        return WindowsCompatFilesystemBackend(root_dir=root_dir, virtual_mode=True)
    return FilesystemBackend(root_dir=root_dir, virtual_mode=True)
