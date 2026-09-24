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
``...(argument truncated)`` marker is display truncation only. Issue #247
extends the trailer with the change region: a successful edit_file states
``changed_lines: A-B`` (computed by comparing the pre-edit read against the
post-edit read-back, both taken at the tool boundary) plus a short excerpt of
the region's leading lines, so confirming an edit never needs a whole-file
re-read; a successful write_file — which replaced every line — reports
``changed_lines: 1-N``.

Two grep behaviors (issue #218): ``ArcCompositeBackend`` replaces the stock
``CompositeBackend`` in the build path and expands a pattern containing `|`
into literal alternatives, searching each branch with upstream's own engines
and merging the structured matches — so every output mode, permission filter
and max_count truncation keeps upstream behavior. ``GrepGuidanceMiddleware``
appended next to ``PermissionDeniedHintMiddleware`` replaces upstream's
no-match regex hint (whose "run a separate search per alternative" advice
coached the per-keyword call loop the expansion removes) with text that
matches the actual semantics, and injects a strategy-change prompt once one
search scope accumulates consecutive no-match results.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deepagents import __version__ as _deepagents_version
from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.backends.protocol import GrepMatch, GrepResult
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

# Same posture: the guidance middleware below rewrites this upstream note at
# runtime (computed from the function itself, not a copied string, so upstream
# wording drift keeps being stripped) — a rename must fail the build loudly
# instead of letting the loop-coaching advice resurface silently.
from deepagents.backends.utils import (
    regex_literal_hint as _upstream_regex_literal_hint,
)

from core.path_compat import (
    normalize_windows_extended_prefix_path,
    normalize_windows_extended_prefix_text,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

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


# -- change region (issue #247) ------------------------------------------------
#
# A model that wants to confirm an edit re-reads the file it just edited — a
# re-read the post-write budget then blocks, turning the confirmation into a
# stall. The success receipt therefore states where the change landed:
# `changed_lines: A-B`, plus a short excerpt of the region's leading lines.
# For edit_file the span is computed by comparing the pre-edit read against
# the post-edit read-back (both taken at the tool boundary, the freshest disk
# truth on either side of the write) — deriving it from the call args cannot
# distinguish a landed edit from a coincidental match under replace_all. For
# write_file the written content is itself the exact disk truth (PR #134
# hashes it the same way), so the region is just its full line span.

_CHANGED_LINES_PREFIX = "changed_lines"
_EXCERPT_HEADER = "changed_excerpt:"
_EXCERPT_INDENT = "    "
_EXCERPT_ELLIPSIS = "…"
_EXCERPT_MAX_LINES = 3
_EXCERPT_MAX_CHARS = 120


def _file_lines(text: str) -> "list[str]":
    """``text`` as the lines upstream's read would count (``splitlines``).

    A trailing terminator closes the last line, it does not open an empty
    one, and an empty string has no lines at all (``"".splitlines() ==
    []``, where a bare ``split("\\n")`` would return one empty line); both
    the write region's ``1-N`` span and the edit excerpt's line indexing
    follow this convention.
    """

    if not text:
        return []
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()  # the artifact of the trailing terminator, not a line
    return lines


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


def _write_region_lines(content: str) -> "list[str]":
    """The ``changed_lines`` entry for a write_file receipt.

    A write replaces the entire file, so the change region is every line of
    the content as persisted. An empty write has no lines to span.
    """

    lines = _file_lines(content)
    if not lines:
        return []
    return [f"{_CHANGED_LINES_PREFIX}: 1-{len(lines)}"]


def _common_prefix_len(before: str, after: str) -> int:
    limit = min(len(before), len(after))
    index = 0
    while index < limit and before[index] == after[index]:
        index += 1
    return index


def _common_suffix_len(before: str, after: str, *, cap: int) -> int:
    """Longest common tail, capped so it never meets the common prefix."""

    index = 0
    while index < cap and before[len(before) - 1 - index] == after[len(after) - 1 - index]:
        index += 1
    return index


def _changed_region_lines(before: "str | None", after: str) -> "list[str]":
    """``changed_lines`` (+ excerpt) describing how ``after`` differs from ``before``.

    Both sides are utf-8 text read through the backend's universal-newline
    read path — the pre-edit state may be CRLF on disk, but it compares
    against the LF-only post-edit rewrite like-for-like. ``before is None``
    (unreadable pre-edit state) and ``before == after`` (a normalized no-op
    replacement) yield no region lines rather than a guessed one.
    """

    if before is None or before == after:
        return []
    prefix = _common_prefix_len(before, after)
    suffix = _common_suffix_len(before, after, cap=min(len(before), len(after)) - prefix)
    start_line = after.count("\n", 0, prefix) + 1
    # Line of the last changed character. For a pure deletion the span in
    # `after` is empty (prefix + suffix covers the whole surviving text); the
    # max() then pins the seam line where the deleted content used to sit.
    end_line = after.count("\n", 0, max(prefix, len(after) - suffix - 1)) + 1
    return [f"{_CHANGED_LINES_PREFIX}: {start_line}-{end_line}", *_excerpt_lines(after, start_line, end_line)]


def _excerpt_lines(after: str, start_line: int, end_line: int) -> "list[str]":
    """The leading lines of the reported region, capped for receipt size.

    Line numbers index ``after`` the way upstream's read counts lines, so the
    excerpt shows exactly what ``read_file(offset=start_line, ...)`` would.
    """

    lines = _file_lines(after)
    window = lines[start_line - 1 : end_line]
    excerpt = [_EXCERPT_HEADER]
    for line in window[:_EXCERPT_MAX_LINES]:
        truncated = line[:_EXCERPT_MAX_CHARS] + _EXCERPT_ELLIPSIS if len(line) > _EXCERPT_MAX_CHARS else line
        excerpt.append(f"{_EXCERPT_INDENT}{truncated}" if truncated else "")
    if len(window) > _EXCERPT_MAX_LINES:
        excerpt.append(_EXCERPT_INDENT + _EXCERPT_ELLIPSIS)
    return excerpt


def _read_back_body(read_back: Any) -> "str | None":
    """The utf-8 text of a backend read result, or ``None`` when unfaithful.

    Error reads, binary payloads and the empty/whitespace-only reminder (which
    carries no pagination metadata) all degrade to ``None``: a receipt line is
    appended only when the read is known to describe the disk truth.
    """

    if read_back is None or getattr(read_back, "error", None) is not None:
        return None
    file_data = getattr(read_back, "file_data", None)
    if not isinstance(file_data, dict) or file_data.get("encoding") != "utf-8":
        return None
    body = file_data.get("content")
    if not isinstance(body, str) or getattr(read_back, "total_lines", None) is None:
        return None
    return body


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
    placeholder" and triggers delete-rewrite loops. The edit receipt also
    states the change region (``changed_lines: A-B`` computed from the pre-
    and post-edit disk reads, plus a capped excerpt) so the post-edit
    confirmation never motivates a whole-file re-read, which the post-write
    budget would block anyway (issue #247).
    """

    # Matches the stock core-stack entry so ``create_deep_agent`` replaces it
    # in place instead of appending a second filesystem middleware.
    name = "FilesystemMiddleware"

    def __init__(
        self,
        *,
        custom_tool_descriptions: "Mapping[str, str] | None" = None,
        **kwargs: Any,
    ) -> None:
        # `custom_tool_descriptions` is upstream's supported description seam
        # (it also short-circuits the request-time execution-visibility
        # rewrite, which would otherwise re-derive the stock text). ARC only
        # overrides grep; every other tool keeps its stock description.
        descriptions = dict(custom_tool_descriptions or {})
        descriptions.setdefault("grep", ARC_GREP_TOOL_DESCRIPTION)
        super().__init__(custom_tool_descriptions=descriptions, **kwargs)

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
            return _with_receipt_trailer(
                result,
                [*_integrity_lines(content), *_write_region_lines(content), _WRITE_RECEIPT_NOTE],
            )

        return sync_write

    def _awrite_with_integrity_trailer(
        self,
        upstream_write: "Callable[..., Any]",
    ) -> "Callable[..., Any]":
        async def async_write(file_path: str, content: str, runtime: ToolRuntime) -> ToolMessage:
            result = await upstream_write(file_path=file_path, content=content, runtime=runtime)
            return _with_receipt_trailer(
                result,
                [*_integrity_lines(content), *_write_region_lines(content), _WRITE_RECEIPT_NOTE],
            )

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
            before_body = _read_back_body(self._read_back(file_path))
            result = upstream_edit(
                file_path=file_path,
                old_string=old_string,
                new_string=new_string,
                runtime=runtime,
                replace_all=replace_all,
            )
            return _with_receipt_trailer(
                result,
                self._edit_trailer_lines(result, before_body, _read_back_body(self._read_back(file_path))),
            )

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
            before_body = _read_back_body(await self._aread_back(file_path))
            result = await upstream_edit(
                file_path=file_path,
                old_string=old_string,
                new_string=new_string,
                runtime=runtime,
                replace_all=replace_all,
            )
            return _with_receipt_trailer(
                result,
                self._edit_trailer_lines(
                    result, before_body, _read_back_body(await self._aread_back(file_path))
                ),
            )

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
    def _edit_trailer_lines(tool_result: Any, before_body: "str | None", after_body: "str | None") -> "list[str]":
        """Integrity + change-region trailer for an edit receipt.

        Unlike write_file, the edited content is upstream's internal
        composition (its universal-newline read plus replacement), so the only
        truthful source is the file itself. After a successful edit the disk
        bytes are LF-only (upstream rewrites the whole file with
        ``newline=""``), so the line-window read-back is byte-faithful — pinned
        by test in ``tests/test_agents/test_write_receipt_integrity.py``.

        A windowed or non-text read-back (error, binary payload; empty or
        whitespace-only files carry no pagination metadata and a reminder
        string instead of content) degrades to the note-only trailer: a
        successful edit must never produce a missing or lying receipt. An
        unreadable *pre-edit* read only drops the change-region lines — the
        post-edit integrity lines stay — and a no-op replacement (pre- and
        post-edit text identical after normalization) reports no region.
        """

        if _success_text(tool_result) is None:
            return []
        if after_body is None:
            return [_WRITE_RECEIPT_NOTE]
        return [
            *_integrity_lines(after_body),
            *_changed_region_lines(before_body, after_body),
            _WRITE_RECEIPT_NOTE,
        ]


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


# -- grep: literal-alternation expansion and no-match guidance (issue #218) -----


#: Upper bound on literal alternatives expanded from one `|` pattern. The
#: serial-4 run's misuse peaked around three alternatives; 8 leaves generous
#: headroom while bounding the per-call fan-out (each branch is a full search).
MAX_GREP_ALTERNATIVES = 8


#: ARC's replacement for upstream's grep tool description. The stock text ends
#: with "To match any of several strings, run a separate grep for each" — the
#: per-keyword call loop issue #218 removes — and denies the `|` expansion the
#: backend now performs, so the injected description must state the actual
#: semantics on every surface the model reads. The cap is interpolated from
#: MAX_GREP_ALTERNATIVES so the prose cannot drift from the enforced limit.
ARC_GREP_TOOL_DESCRIPTION = f"""Search for a LITERAL text pattern across files (NOT regex).

The pattern is matched verbatim: regex metacharacters are ordinary characters, not operators (`.*`, `\\.`, `^`, `$` are searched as plain text). A pattern containing `|` is expanded into literal alternatives: `grep(pattern="foo|bar")` matches files containing `foo` OR `bar` (at most {MAX_GREP_ALTERNATIVES} alternatives per call; write `\\|` to search for a literal `|` instead). Do not enumerate keyword guesses one grep at a time — when a search misses, read the candidate file (`read_file` with offset/limit) or search one distinctive literal copied from earlier tool output.

Returns matching files or content per `output_mode`. Offloaded large tool results live under the artifacts root (`/large_tool_results/` by default); grep that directory to search them when you do not know the exact path."""

#: Upstream renders an empty grep result as exactly this sentinel line.
_GREP_NO_MATCH_SENTINEL = "No matches found"

#: Consecutive no-match greps on one scope before the budget hint fires, and
#: the count at which its wording escalates. The hint is advisory only: the
#: 2026-09-22 serial-4 REQ-1 run burned 88 greps (about half alternation
#: misuse) without any signal to change strategy.
_GREP_NO_MATCH_HINT_AFTER = 3
_GREP_NO_MATCH_ESCALATE_AFTER = 2 * _GREP_NO_MATCH_HINT_AFTER


@dataclass(frozen=True)
class GrepAlternation:
    """A pattern's literal-alternation expansion, shared by backend and guidance.

    ``searched`` holds the deduplicated, trimmed branches the backend runs
    (capped at ``MAX_GREP_ALTERNATIVES``); ``total`` is the pre-cap branch
    count. ``expanded`` is false for a single-branch result (e.g. `a\\|b`
    unescaping to the literal `a|b`): the search differs from the raw pattern,
    but no alternation happened, so the guidance must not claim one.
    """

    searched: tuple[str, ...]
    total: int

    @property
    def expanded(self) -> bool:
        return self.total > 1

    @property
    def dropped(self) -> int:
        return self.total - len(self.searched)


def split_literal_alternation(pattern: str) -> list[str] | None:
    """Split a grep pattern on `|` into literal alternatives.

    deepagents' grep matches literal text, so a model-written `a|b|c` searches
    for the literal characters and silently misses — upstream's result note
    then advises "run a separate search per alternative", which coached the
    per-keyword LLM-call archaeology loop this expansion removes. The split is
    purely lexical: each branch is itself a literal pattern (no regex), a
    backslash-escaped `\\|` unescapes to a literal `|` (the documented escape
    hatch for searching a real pipe), branches are trimmed and deduplicated
    keeping first-seen order, and empty branches (from `a||b` or a dangling
    `|`) are dropped.

    Returns ``None`` when the pattern contains no `|` at all — the caller then
    searches the raw pattern with upstream behavior unchanged — or the
    effective branch list (one or more literals) otherwise.
    """

    pattern = str(pattern)
    if "|" not in pattern:
        return None
    branches: list[str] = []
    current: list[str] = []
    escaped = False
    for char in pattern:
        if escaped:
            # Only `\|` is special: any other backslash sequence stays literal.
            if char == "|":
                current.append("|")
            else:
                current.append("\\")
                current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            branches.append("".join(current))
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    branches.append("".join(current))
    cleaned: list[str] = []
    for branch in branches:
        branch = branch.strip()
        if branch and branch not in cleaned:
            cleaned.append(branch)
    return cleaned or None


def alternation_expansion(pattern: str) -> GrepAlternation | None:
    """The pattern's expansion, or ``None`` when there is nothing to expand."""

    branches = split_literal_alternation(pattern)
    if branches is None:
        return None
    return GrepAlternation(
        searched=tuple(branches[:MAX_GREP_ALTERNATIVES]),
        total=len(branches),
    )


def _merge_alternation_results(
    results: list[GrepResult],
    *,
    max_count: int | None,
) -> GrepResult:
    """Merge per-branch grep results into one, deduplicating by (path, line).

    Structured merging keeps every downstream behavior upstream-owned: the
    tool boundary formats all output modes from the merged match list, the
    permission filter still redacts, and the truncation note still renders.
    A merged result is flagged ``truncated`` when any branch was, or when the
    cap was reached — reaching the cap across branches proves nothing about
    the unsearched remainder, mirroring upstream's conservative composite
    semantics. Branch errors are deduplicated and joined so an error that
    affected every branch (a refused glob, a bad path) still renders as an
    error result.
    """

    matches: list[GrepMatch] = []
    seen: set[tuple[str, int]] = set()
    errors: list[str] = []
    truncated = False
    for result in results:
        truncated = truncated or result.truncated
        if result.error and result.error not in errors:
            errors.append(result.error)
        for match in result.matches or []:
            key = (str(match.get("path", "")), int(match.get("line", 0) or 0))
            if key in seen:
                continue
            if max_count is not None and len(matches) >= max_count:
                truncated = True
                break
            seen.add(key)
            matches.append(match)
    return GrepResult(error="\n".join(errors) or None, matches=matches, truncated=truncated)


class ArcCompositeBackend(CompositeBackend):
    """Composite backend that expands `|` patterns into literal branch searches.

    Mounted at the composite seam (not per-route) so every routed backend and
    the default one share the semantics: a pathless grep scans routes *and*
    the default backend, and expanding inside one route only would make the
    same pattern alternation-aware in one part of the result and literal-only
    in another. Each branch runs upstream's own grep (ripgrep `-F` or the
    Python fallback) with the caller's ``max_count`` passed through, so
    per-branch cost stays bounded; the structured matches are then merged once.
    """

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        expansion = alternation_expansion(pattern)
        if expansion is None:
            return super().grep(pattern, path=path, glob=glob, max_count=max_count)
        results = [
            super().grep(branch, path=path, glob=glob, max_count=max_count)
            for branch in expansion.searched
        ]
        return _merge_alternation_results(results, max_count=max_count)

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        expansion = alternation_expansion(pattern)
        if expansion is None:
            return await super().agrep(pattern, path=path, glob=glob, max_count=max_count)
        results = [
            await super().agrep(branch, path=path, glob=glob, max_count=max_count)
            for branch in expansion.searched
        ]
        return _merge_alternation_results(results, max_count=max_count)


def _quote_branches(branches: tuple[str, ...], *, limit: int = 6) -> str:
    shown = ", ".join(f"`{branch}`" for branch in branches[:limit])
    if len(branches) > limit:
        shown += f", … (+{len(branches) - limit} more)"
    return shown


def _expansion_note(expansion: GrepAlternation) -> str:
    """The one-line note that states what an expanded pattern searched."""

    if expansion.dropped:
        head = (
            f"Note: the pattern had {expansion.total} alternatives; searched the "
            f"first {len(expansion.searched)} (cap {MAX_GREP_ALTERNATIVES}): "
            f"{_quote_branches(expansion.searched)}."
        )
    else:
        head = (
            f"Note: the pattern was expanded as {expansion.total} literal "
            f"alternatives: {_quote_branches(expansion.searched)}."
        )
    return (
        f"{head} Results are the union across alternatives; write `\\|` to "
        "search for a literal `|`."
    )


def _no_match_note(expansion: GrepAlternation | None) -> str:
    """The no-match note that replaces upstream's loop-coaching regex hint."""

    if expansion is not None and expansion.expanded:
        if expansion.dropped:
            head = (
                f"Note: none of the first {len(expansion.searched)} of "
                f"{expansion.total} alternatives "
                f"({_quote_branches(expansion.searched)}) matched."
            )
        else:
            head = (
                f"Note: none of the {expansion.total} literal alternatives "
                f"({_quote_branches(expansion.searched)}) matched."
            )
    else:
        head = "Note: no file contains that pattern as literal text."
    return (
        f"{head} grep matches literal text, not regex: metacharacters like "
        "`.*` and `\\.` are searched verbatim. Search a distinctive literal "
        "copied from earlier file output, or read_file the likely file, "
        "instead of retrying keyword variants."
    )


def _budget_note(streak: int, scope: str) -> str:
    """The strategy-change prompt for a scope accumulating no-match greps."""

    if streak >= _GREP_NO_MATCH_ESCALATE_AFTER:
        return (
            f"No-match budget: {streak} consecutive no-match greps on {scope}. "
            "The text you keep guessing for is very likely absent from this "
            "scope — stop searching it; read the file that should contain it "
            "and verify, or change strategy."
        )
    return (
        f"No-match budget: {streak} consecutive no-match greps on {scope}. "
        "Stop enumerating keyword guesses — read the candidate file "
        "(`read_file` with offset/limit) or confirm the file exists "
        "(`ls`/`glob`) before another search."
    )


def _strip_upstream_regex_note(content: str, pattern: str) -> str:
    """Cut upstream's regex hint from a grep result, if it was appended.

    The note text is computed from upstream's own function rather than copied,
    so wording drift on the upstream side keeps being stripped. If upstream
    stops emitting the note entirely, this is a no-op — the ARC note below is
    appended unconditionally on no-match, so the semantics never regress to
    the loop-coaching advice.
    """

    note = _upstream_regex_literal_hint(pattern)
    if note and content.endswith(note):
        return content[: -len(note)].rstrip()
    return content


class GrepGuidanceMiddleware(AgentMiddleware[Any, Any, Any]):
    """Keep grep results honest about their semantics and bound no-match loops.

    Two rewrites on the tool boundary, both keyed off the call's own args:

    - The upstream no-match regex hint (emitted whenever a pattern carries
      regex signals — including every `a|b` alternation) ends with "for `|`
      alternation, run a separate search per alternative". With
      ``ArcCompositeBackend`` expanding alternation that advice is wrong on
      both counts, and it is the sentence that coached the serial-4 REQ-1
      loop. It is stripped and replaced with the matching-semantics note
      above; matching results from an expanded pattern gain the one-line
      expansion note so the model learns the semantics from the result.
    - A per-scope streak counter turns repeated no-match greps on the same
      path into an escalating strategy-change prompt. The state lives on the
      middleware instance (one per built stage agent — a session's
      conversation scope) and resets when that scope returns a match; error
      results neither count nor reset.

    Best-effort like the usage capture: any failure leaves the tool result
    untouched rather than breaking the call.
    """

    def __init__(self) -> None:
        self._no_match_streaks: dict[str, int] = {}

    def wrap_tool_call(self, request: "ToolCallRequest", handler: Any) -> Any:
        return self._with_guidance(request, handler(request))

    async def awrap_tool_call(self, request: "ToolCallRequest", handler: Any) -> Any:
        return self._with_guidance(request, await handler(request))

    def _with_guidance(self, request: "ToolCallRequest", result: Any) -> Any:
        try:
            call = getattr(request, "tool_call", None) or {}
            if str(call.get("name") or "") != "grep":
                return result
            content = getattr(result, "content", None)
            if not isinstance(content, str):
                return result
            if str(getattr(result, "status", "") or "") == "error":
                # Errors (denied paths, refused globs) are not no-matches:
                # they must neither advance nor reset the budget streak.
                return result
            args = call.get("args") or {}
            pattern = str(args.get("pattern") or "")
            scope = str(args.get("path") or "").strip().rstrip("/") or "(default root)"
            expansion = alternation_expansion(pattern)
            no_match = content.split("\n\n", 1)[0] == _GREP_NO_MATCH_SENTINEL
            if no_match:
                streak = self._no_match_streaks.get(scope, 0) + 1
                self._no_match_streaks[scope] = streak
            else:
                streak = 0
                self._no_match_streaks[scope] = 0

            notes: list[str] = []
            if no_match:
                notes.append(_no_match_note(expansion))
            elif expansion is not None and expansion.expanded:
                notes.append(_expansion_note(expansion))
            if streak >= _GREP_NO_MATCH_HINT_AFTER:
                notes.append(_budget_note(streak, scope))
            if not notes:
                return result
            updated = _strip_upstream_regex_note(content, pattern)
            result.content = updated + "\n\n" + "\n\n".join(notes)
            return result
        except Exception:
            logger.debug("grep guidance rewrite failed", exc_info=True)
            return result
