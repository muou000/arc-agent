from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from deepagents import GeneralPurposeSubagentProfile, FilesystemPermission, HarnessProfile, create_deep_agent, register_harness_profile
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents._models import get_model_provider
from deepagents.backends.filesystem import _raise_if_symlink_loop
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel, Field, create_model

from agents.model.factory import create_arc_chat_model
from agents.model.openai_api_adapter import structured_output_supported
from agents.runtime.checkpointer import get_checkpointer
from agents.runtime.contracts import AgentRuntimeContext
from agents.runtime.stage_discipline import StageDisciplineMiddleware
from agents.runtime.tool_usage import ToolUsageMiddleware
from core.path_compat import normalize_windows_extended_prefix_path, normalize_windows_extended_prefix_text

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from langchain.agents.middleware.types import ModelRequest, ModelResponse, ResponseT, ToolCallRequest
    from langchain_core.tools import BaseTool


WORKSPACE_PREFIX = "/workspace"
SKILLS_PREFIX = "/skills"
DISABLED_BUILTIN_TOOLS = frozenset({"execute", "write_todos"})
_WINDOWS_PATH_COMPAT_APPLIED = False
_READ_FILE_FORMAT_PATCHED = False
_DELETE_NOT_FOUND_PATCHED = False

# Sentinel so callers can explicitly pass ``checkpointer=None`` (cold start)
# while omitting the argument still resolves the process-wide shared saver.
_UNSET: Any = object()

# Provider keys whose ARC harness profile has already been registered.
_REGISTERED_HARNESS_PROFILES: set[str] = set()

class OpenAIGlobSchema(BaseModel):
    """OpenAI-compatible schema for the glob tool."""

    pattern: str = Field(description="Glob pattern to match files (e.g., '**/*.py', '*.txt', '/subdir/**/*.md').")
    path: str = Field(
        default=None,
        description=(
            "Required base directory to search from, as an absolute virtual path "
            "(e.g. '/workspace', '/workspace/backend/tests'). A glob without `path` "
            "is denied by the read policy, so always pass it explicitly."
        ),
    )


class OpenAIGrepSchema(BaseModel):
    """OpenAI-compatible schema for the grep tool."""

    pattern: str = Field(description="Text pattern to search for (literal string, not regex).")
    path: str = Field(default=None, description="Base directory to search from. Defaults to the backend's default root.")
    glob: str = Field(
        default=None,
        description="Glob pattern to filter which files to search (e.g., '*.py').",
    )
    output_mode: Literal["files_with_matches", "content", "count"] = Field(
        default="files_with_matches",
        description="Output format: 'files_with_matches' (file paths only, default), 'content' (matching lines with context), 'count' (match counts per file).",
    )


_TEXT_ENVELOPE_KEYS = frozenset({"$text", "text", "content"})


def _unwrap_text_envelope(value: Any) -> Any:
    """Undo a single-key text envelope such as ``{"$text": "..."}``.

    Models occasionally wrap long file payloads in an object instead of passing
    the string directly. Only the unambiguous single-string-key shape is
    unwrapped; anything else is left for the caller to reject.
    """

    if not isinstance(value, dict) or len(value) != 1:
        return value
    (key, inner), = value.items()
    if key in _TEXT_ENVELOPE_KEYS and isinstance(inner, str):
        return inner
    return value


class ToolArgumentSanitizerMiddleware(AgentMiddleware[Any, Any, Any]):
    """Fail fast when a model submits non-string payloads to file tools.

    Some models occasionally wrap file content in an envelope object
    (``content={"$text": "..."}`` was observed on the 12306 benchmark). The
    filesystem tool accepted the envelope, wrote it to disk, and the model then
    spent dozens of turns repairing the file it had just corrupted. Unwrap
    single-key text envelopes here, and reject anything else with an explicit
    tool message instead of silently stringifying garbage into the workspace.
    """

    _TEXT_ARGS_BY_TOOL = {
        "write_file": ("content",),
        "edit_file": ("old_string", "new_string"),
    }

    def wrap_tool_call(
        self,
        request: "ToolCallRequest",
        handler: "Callable[[ToolCallRequest], Any]",
    ) -> Any:
        prepared = self._prepare(request)
        if isinstance(prepared, ToolMessage):
            return prepared
        return handler(prepared)

    async def awrap_tool_call(
        self,
        request: "ToolCallRequest",
        handler: "Callable[[ToolCallRequest], Awaitable[Any]]",
    ) -> Any:
        prepared = self._prepare(request)
        if isinstance(prepared, ToolMessage):
            return prepared
        return await handler(prepared)

    def _prepare(self, request: "ToolCallRequest") -> Any:
        """Return the request to execute (sanitized) or a rejection message."""

        call = request.tool_call
        tool_name = str(call.get("name") or "")
        arg_names = self._TEXT_ARGS_BY_TOOL.get(tool_name)
        if not arg_names:
            return request
        args = call.get("args")
        if not isinstance(args, dict):
            return request

        patch: dict[str, Any] = {}
        for arg_name in arg_names:
            if arg_name not in args:
                continue
            value = args[arg_name]
            if isinstance(value, str):
                continue
            unwrapped = _unwrap_text_envelope(value)
            if isinstance(unwrapped, str):
                patch[arg_name] = unwrapped
            else:
                return ToolMessage(
                    content=(
                        f"Error: `{tool_name}` argument `{arg_name}` must be a plain string, "
                        f"but a {type(value).__name__} was received. Re-send the tool call and "
                        "pass the file text directly as the argument value."
                    ),
                    tool_call_id=str(call.get("id") or ""),
                )
        if not patch:
            return request
        return request.override(tool_call={**call, "args": {**args, **patch}})


_LENGTH_FINISH_REASONS = frozenset({"length"})


def _message_hit_output_limit(message: Any) -> bool:
    """Return True when the provider cut this response short of completion.

    Chat-completions responses carry ``finish_reason="length"`` in
    ``response_metadata``; Responses-API responses carry
    ``status="incomplete"``. Some proxies pass only
    ``incomplete_details`` through, so a populated
    ``incomplete_details.reason`` (which only exists on incomplete
    responses) counts as a cut on its own. Either way the payload may end
    mid-argument.
    """

    metadata = getattr(message, "response_metadata", None)
    if not isinstance(metadata, dict):
        return False
    if metadata.get("finish_reason") in _LENGTH_FINISH_REASONS:
        return True
    if metadata.get("status") == "incomplete":
        return True
    details = metadata.get("incomplete_details")
    return isinstance(details, dict) and bool(details.get("reason"))


def _output_cut_reason(message: Any) -> str:
    """Human-readable cause of the cut, for the rejection wording."""

    metadata = getattr(message, "response_metadata", None)
    if not isinstance(metadata, dict):
        return "the assistant response was cut off before completion"
    if metadata.get("finish_reason") in _LENGTH_FINISH_REASONS:
        return "the assistant response hit the output token limit"
    details = metadata.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    if reason == "content_filter":
        return "the assistant response was cut off by the provider's content policy"
    if reason:
        return f"the assistant response hit the output token limit ({reason})"
    return "the assistant response was cut off before completion"


def _truncation_rejection_tool_message(call: "Mapping[str, Any]", cut_reason: str) -> ToolMessage:
    """Error tool result for a tool call issued inside a truncated response."""

    parse_error = call.get("error")
    detail = f" Its arguments could not be parsed ({parse_error})." if parse_error else ""
    advice = (
        "Adjust the output content and re-issue the tool call."
        if "content policy" in cut_reason
        else "Re-issue the tool call with complete arguments."
    )
    return ToolMessage(
        content=(
            f"Error: tool call `{call.get('name') or '<unknown>'}` was not executed: "
            f"{cut_reason}, so its arguments may be truncated.{detail} {advice}"
        ),
        tool_call_id=str(call.get("id") or ""),
        status="error",
    )


class TruncatedToolCallGuardMiddleware(AgentMiddleware[Any, Any, Any]):
    """Fail every tool call carried by an output-truncated model response.

    A response cut off by the output token limit can end mid-argument, and a
    truncated tool-call payload may still parse and pass schema validation,
    silently corrupting the workspace (pi's ``failToolCallsFromTruncatedMessage``
    lesson). Rewrite the response so every unanswered tool call in the truncated
    message gets an explicit error tool result; the router then hands control
    back to the model, which re-issues the calls with complete arguments.

    The truncated AIMessage deliberately KEEPS its ``tool_calls``: it is the
    only dispatch source (langchain's model-to-tools edge fires on unanswered
    ``tool_calls``), and keeping them present-but-covered is what routes
    control back to the model with the error results in context. Stripping
    them would end the run without a reissue, and downstream post-processing
    (tool-batch logs, trace formatting) pairs those calls with the injected
    error results by id.
    """

    def wrap_model_call(
        self,
        request: "ModelRequest[Any]",
        handler: "Callable[[ModelRequest[Any]], ModelResponse[Any]]",
    ) -> "ModelResponse[Any]":
        return self._fail_truncated_tool_calls(handler(request))

    async def awrap_model_call(
        self,
        request: "ModelRequest[Any]",
        handler: "Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]]",
    ) -> "ModelResponse[Any]":
        return self._fail_truncated_tool_calls(await handler(request))

    def _fail_truncated_tool_calls(self, response: Any) -> Any:
        result = getattr(response, "result", None)
        if not isinstance(result, list) or not result:
            return response
        answered = {
            str(message.tool_call_id)
            for message in result
            if isinstance(message, ToolMessage) and message.tool_call_id
        }
        guarded: list[Any] = []
        truncated = False
        for message in result:
            guarded.append(message)
            if not isinstance(message, AIMessage) or not _message_hit_output_limit(message):
                continue
            rejections = self._rejections_for(message, answered)
            if not rejections:
                continue
            guarded.extend(rejections)
            truncated = True
        if not truncated:
            return response
        return replace(response, result=guarded)

    def _rejections_for(self, message: AIMessage, answered: set[str]) -> list[ToolMessage]:
        cut_reason = _output_cut_reason(message)
        rejections: list[ToolMessage] = []
        for call in (*message.tool_calls, *message.invalid_tool_calls):
            call_id = str(call.get("id") or "")
            if not call_id or call_id in answered:
                continue
            answered.add(call_id)
            rejections.append(_truncation_rejection_tool_message(call, cut_reason))
        return rejections


class DisableToolsMiddleware(AgentMiddleware[Any, Any, Any]):
    """Hide selected tools from model requests."""

    def __init__(self, *, disabled: frozenset[str]) -> None:
        self._disabled = disabled

    def wrap_model_call(
        self,
        request: "ModelRequest[Any]",
        handler: "Callable[[ModelRequest[Any]], ModelResponse[Any]]",
    ) -> "ModelResponse[Any]":
        return handler(request.override(tools=self._filter_tools(request.tools)))

    async def awrap_model_call(
        self,
        request: "ModelRequest[Any]",
        handler: "Callable[[ModelRequest[Any]], Awaitable[ModelResponse[ResponseT]]]",
    ) -> "ModelResponse[ResponseT]":
        return await handler(request.override(tools=self._filter_tools(request.tools)))

    def _filter_tools(self, tools: list[Any]) -> list[Any]:
        return [
            _normalize_tool_schema(tool)
            for tool in tools
            if _tool_name(tool) not in self._disabled
        ]


def build_stage_agent(
    *,
    name: str,
    stage: Literal["interface_design", "test_generation", "implementation"],
    model: str | object,
    system_prompt: str,
    response_format: object | None,
    workspace_root: str,
    writable_roots: list[str],
    skills: list[str] | None = None,
    permitted_skill_names: list[str] | None = None,
    memory: list[str] | None = None,
    tools: list[object] | None = None,
    checkpointer: Any = _UNSET,
    node_id: str | None = None,
    claims_workspace_root: str | None = None,
    test_manifest_lock: Any | None = None,
):
    """Create an agent instance with ARC's first-batch filesystem policy.

    ``checkpointer`` defaults to the process-wide shared saver so that rebuilding
    an agent for the same ``thread_id`` resumes the previous conversation rather
    than starting cold. Pass ``checkpointer=None`` to opt a single agent out.

    ``test_manifest_lock`` wires the test_generation stage's manifest-first
    gate (see ``agents/tools/test_manifest.py``); other stages ignore it.
    """

    _apply_windows_filesystem_path_compat()
    _apply_unambiguous_read_file_format()
    _apply_delete_not_found_precedence()
    resolved_checkpointer = get_checkpointer() if checkpointer is _UNSET else checkpointer
    root = Path(workspace_root).expanduser().resolve()
    routes = {
        f"{WORKSPACE_PREFIX}/": FilesystemBackend(
            root_dir=str(root),
            virtual_mode=True,
        ),
    }
    skills_root = _compiler_skills_root()
    if skills_root.exists():
        routes[f"{SKILLS_PREFIX}/"] = FilesystemBackend(
            root_dir=str(skills_root),
            virtual_mode=True,
        )
    backend = CompositeBackend(default=StateBackend(), routes=routes)

    resolved_model = create_arc_chat_model(model)
    _register_arc_tool_exclusions(model=model, resolved_model=resolved_model)
    resolved_skills = _resolve_source_paths(skills, root, skills_root, default=[f"{SKILLS_PREFIX}/"])

    file_claim_gate = None
    if node_id and claims_workspace_root:
        # Parallel-worktree ownership guard for new files. The two roots are
        # deliberately different:
        # - ``claims_workspace_root`` is the *integration* workspace, where
        #   the shared claim registry lives; every in-flight node resolves
        #   the same registry through it. In parallel mode the per-task
        #   runner always injects it (``context_workspace_root``); in serial
        #   mode it equals the one shared workspace.
        # - ``agent_root`` is *this agent's* filesystem root — the task
        #   worktree in parallel mode — and defines what "tracked" means.
        #   A sibling's committed-but-unmerged file is untracked here
        #   precisely because its branch is invisible in this worktree;
        #   that is the arbitration the claims provide.
        from core.file_claims import FileClaimGate, get_file_claim_registry

        file_claim_gate = FileClaimGate(
            get_file_claim_registry(claims_workspace_root),
            node_id=node_id,
            agent_root=str(root),
        )

    stage_discipline = StageDisciplineMiddleware(
        stage=stage,
        file_claim_gate=file_claim_gate,
        test_manifest_lock=test_manifest_lock if stage == "test_generation" else None,
    )
    agent = create_deep_agent(
        name=name,
        model=resolved_model,
        backend=backend,
        system_prompt=system_prompt,
        middleware=[
            ToolUsageMiddleware(),
            TruncatedToolCallGuardMiddleware(),
            ToolArgumentSanitizerMiddleware(),
            stage_discipline,
            DisableToolsMiddleware(disabled=DISABLED_BUILTIN_TOOLS),
        ],
        tools=tools or [],
        skills=resolved_skills,
        memory=_resolve_source_paths(memory, root, skills_root, default=[]),
        permissions=_build_filesystem_permissions(
            root,
            writable_roots,
            skill_instruction_paths=_resolve_skill_instruction_paths(
                resolved_skills,
                skills_root,
                permitted_skill_names=permitted_skill_names,
            ),
        ),
        context_schema=AgentRuntimeContext,
        response_format=_resolve_response_format(response_format, model=model),
        checkpointer=resolved_checkpointer,
    )
    # Surface run-local discipline state (e.g. materialized write paths) to
    # the stage adapter that owns this agent.
    agent.arc_stage_discipline = stage_discipline
    return agent


def _apply_unambiguous_read_file_format() -> None:
    """Remove line-number padding from read_file output before agents copy it.

    The upstream filesystem middleware renders each line as a six-character
    line number plus a tab. Models can mistake that separator for indentation
    and then submit an `edit_file` anchor that cannot match the source.
    """
    global _READ_FILE_FORMAT_PATCHED
    if _READ_FILE_FORMAT_PATCHED:
        return

    import deepagents.middleware.filesystem as filesystem_middleware

    def format_without_line_numbers(content: str | list[str], start_line: int = 1) -> str:
        del start_line
        if isinstance(content, str):
            lines = content.split("\n")
            if lines and lines[-1] == "":
                lines = lines[:-1]
            return "\n".join(lines)
        return "\n".join(content)

    filesystem_middleware.format_content_with_line_numbers = format_without_line_numbers
    _READ_FILE_FORMAT_PATCHED = True


def _apply_delete_not_found_precedence() -> None:
    """Report `not found` for deletes of missing paths instead of deny-rule spam.

    Upstream's delete tool decides *before* the permission check whether the
    target "may have descendants" (a recursive delete must scan every deny rule
    for subtree overlap). ``ls`` answering ``path_not_found`` is not on its
    leaf whitelist, so a missing path is treated as a possibly-populated
    directory: every ``**`` deny pattern (``/**``, ``/workspace/**/node_modules``,
    ...) overlaps, and the model is told "permission denied" with the full deny
    rule list — for a file that does not exist. Observed on the 12306 benchmark
    as 3-4 retries against the same missing path.

    The patch recognizes the backend's explicit ``path_not_found`` answer as
    "nothing to protect": permission resolution then follows the ordinary
    first-matching-rule path (same as ``write_file``). A missing path inside a
    writable root reaches the backend, whose own ``delete`` reports the honest
    ``not found``; a denied path is still refused before the backend runs
    (first matching deny rule), so delete cannot be used to probe which
    protected files exist. Any other ``ls`` outcome keeps upstream's
    conservative descendant check.
    """

    global _DELETE_NOT_FOUND_PATCHED
    if _DELETE_NOT_FOUND_PATCHED:
        return

    import logging

    import deepagents
    import deepagents.middleware.filesystem as filesystem_middleware

    # These helpers are upstream implementation details, not public contract.
    # A deepagents upgrade that renames or removes them must degrade to the
    # old (spammier but harmless) error message, not crash build_stage_agent.
    original_has_descendants = getattr(
        filesystem_middleware, "_delete_target_may_have_descendants", None
    )
    original_ahas_descendants = getattr(
        filesystem_middleware, "_adelete_target_may_have_descendants", None
    )
    if not callable(original_has_descendants) or not callable(original_ahas_descendants):
        logging.getLogger(__name__).warning(
            "deepagents %s no longer exposes the delete descendant helpers; "
            "keeping upstream delete permission behavior",
            getattr(deepagents, "__version__", "unknown"),
        )
        return

    _NOT_FOUND_SUFFIX = ": path_not_found"

    def _missing_by_ls_error(ls_result: Any) -> bool:
        """Whether an ls result is the backends' explicit ``path_not_found``.

        Both shipped producers format the sentinel as exactly
        ``"Path '<path>': path_not_found"`` (FilesystemBackend directly,
        SandboxBackend via its JSON error passthrough), so anchor the match
        to that suffix. A bare substring check would also fire when the
        *path itself* contains the token (e.g. deleting a
        ``/workspace/path_not_found`` directory whose ls fails some other
        way) and wrongly relax the descendant check.
        """

        error = getattr(ls_result, "error", None)
        return error is not None and str(error).endswith(_NOT_FOUND_SUFFIX)

    def _confirmed_missing(backend: Any, target: str) -> bool:
        """Whether ``backend.ls(target)`` explicitly reports ``path_not_found``.

        Any probe failure (unsupported ``ls``, transient I/O error) counts as
        "not confirmed": the caller keeps upstream's conservative answer
        instead of letting the exception escape into the delete tool.
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

    def _delete_target_may_have_descendants(
        backend: Any, target: str, *, permissions_configured: bool
    ) -> bool:
        if original_has_descendants(backend, target, permissions_configured=permissions_configured):
            return not _confirmed_missing(backend, target)
        return False

    async def _adelete_target_may_have_descendants(
        backend: Any, target: str, *, permissions_configured: bool
    ) -> bool:
        if await original_ahas_descendants(backend, target, permissions_configured=permissions_configured):
            return not await _aconfirmed_missing(backend, target)
        return False

    filesystem_middleware._delete_target_may_have_descendants = _delete_target_may_have_descendants
    filesystem_middleware._adelete_target_may_have_descendants = _adelete_target_may_have_descendants
    _DELETE_NOT_FOUND_PATCHED = True


def _build_filesystem_permissions(
    root: Path,
    writable_roots: list[str],
    *,
    skill_instruction_paths: list[str],
) -> list[Any]:
    permissions: list[Any] = [
        FilesystemPermission(
            operations=["read", "write"],
            paths=[
                f"{WORKSPACE_PREFIX}/.arc",
                f"{WORKSPACE_PREFIX}/.arc/**",
                f"{WORKSPACE_PREFIX}/.git",
                f"{WORKSPACE_PREFIX}/.git/**",
                f"{WORKSPACE_PREFIX}/requirements",
                f"{WORKSPACE_PREFIX}/requirements/**",
                f"{WORKSPACE_PREFIX}/**/node_modules",
                f"{WORKSPACE_PREFIX}/**/node_modules/**",
                f"{WORKSPACE_PREFIX}/**/dist",
                f"{WORKSPACE_PREFIX}/**/dist/**",
                f"{WORKSPACE_PREFIX}/**/dist-ssr",
                f"{WORKSPACE_PREFIX}/**/dist-ssr/**",
                f"{WORKSPACE_PREFIX}/**/build",
                f"{WORKSPACE_PREFIX}/**/build/**",
                f"{WORKSPACE_PREFIX}/**/coverage",
                f"{WORKSPACE_PREFIX}/**/coverage/**",
                f"{WORKSPACE_PREFIX}/**/.vite",
                f"{WORKSPACE_PREFIX}/**/.vite/**",
                f"{WORKSPACE_PREFIX}/**/package-lock.json",
                f"{WORKSPACE_PREFIX}/**/yarn.lock",
                f"{WORKSPACE_PREFIX}/**/pnpm-lock.yaml",
                f"{WORKSPACE_PREFIX}/.env",
                f"{WORKSPACE_PREFIX}/.env.*",
                f"{WORKSPACE_PREFIX}/**/.env",
                f"{WORKSPACE_PREFIX}/**/.env.*",
            ],
            mode="deny",
        ),
        FilesystemPermission(
            operations=["read"],
            paths=[WORKSPACE_PREFIX, f"{WORKSPACE_PREFIX}/**"],
            mode="allow",
        ),
    ]

    if skill_instruction_paths:
        permissions.append(
            FilesystemPermission(
                operations=["read"],
                paths=skill_instruction_paths,
                mode="allow",
            )
        )

    write_paths = [
        virtual_path
        for path in writable_roots
        if str(path or "").strip()
        for virtual_path in _expand_write_permission_paths(path, root)
    ]
    if write_paths:
        permissions.append(
            FilesystemPermission(
                operations=["write"],
                paths=write_paths,
                mode="allow",
            )
        )

    permissions.append(
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/**"],
            mode="deny",
        )
    )
    return permissions


def _resolve_skill_instruction_paths(
    sources: list[str],
    skills_root: Path,
    *,
    permitted_skill_names: list[str] | None = None,
) -> list[str]:
    """Allow direct reads only for stage-selected skill instruction files.

    Deep Agents discovers frontmatter by scanning skill *source* directories. ARC
    therefore passes `/skills/` as the source, then constrains model tool access
    to the exact instruction files selected for the current stage.
    """

    if permitted_skill_names is not None:
        paths = [
            f"{SKILLS_PREFIX}/{name}/SKILL.md"
            for name in dict.fromkeys(permitted_skill_names)
            if (skills_root / name / "SKILL.md").is_file()
        ]
        return paths

    paths: list[str] = []
    for source in sources:
        normalized = _normalize_virtual_path(source).rstrip("/")
        if not normalized.startswith(f"{SKILLS_PREFIX}/") and normalized != SKILLS_PREFIX:
            continue
        if normalized == SKILLS_PREFIX:
            for skill_file in skills_root.glob("*/SKILL.md"):
                paths.append(f"{SKILLS_PREFIX}/{skill_file.parent.name}/SKILL.md")
            continue
        if normalized.endswith("/SKILL.md"):
            paths.append(normalized)
        else:
            paths.append(f"{normalized}/SKILL.md")
    return list(dict.fromkeys(paths))


def _expand_write_permission_paths(path: str, root: Path) -> list[str]:
    normalized = _to_virtual_workspace_path(path, root).rstrip("/") or WORKSPACE_PREFIX
    return [normalized, f"{normalized}/**"]


def _register_arc_tool_exclusions(*, model: Any, resolved_model: Any) -> None:
    """Remove built-in agent tools that ARC does not want to expose.

    The profile is a constant, so re-registering it for every agent build is
    pure overhead; remember which provider keys have already been registered.
    """

    profile = HarnessProfile(
        excluded_tools=DISABLED_BUILTIN_TOOLS,
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    )
    registered: set[str] = set()
    if isinstance(model, str):
        provider, model_name = _split_model_name(model)
        if provider:
            for key in (provider, f"{provider}:{model_name}"):
                registered.add(key)
    provider = get_model_provider(resolved_model)
    if provider:
        registered.add(provider)

    for key in registered - _REGISTERED_HARNESS_PROFILES:
        register_harness_profile(key, profile)
    _REGISTERED_HARNESS_PROFILES.update(registered)


def _resolve_response_format(response_format: object | None, *, model: str | object = "") -> object | None:
    """Keep the pydantic response format only when the endpoint supports it.

    Capability is decided by ``structured_output_supported``: an explicit
    ``ARC_STRUCTURED_OUTPUT`` override wins, direct/official-OpenAI endpoints
    are always supported, and custom ``OPENAI_BASE_URL`` endpoints get one
    cached tool-call probe per process (fail-open on inconclusive results).
    """

    if response_format is None:
        return None
    return response_format if structured_output_supported(model) else None


def _apply_windows_filesystem_path_compat() -> None:
    """Normalize Windows extended-length paths before agent containment checks."""

    global _WINDOWS_PATH_COMPAT_APPLIED
    if _WINDOWS_PATH_COMPAT_APPLIED or os.name != "nt":
        return

    original_resolve_path = FilesystemBackend._resolve_path
    original_to_virtual_path = FilesystemBackend._to_virtual_path

    def _resolve_path_with_windows_compat(self: FilesystemBackend, key: str) -> Path:
        if not getattr(self, "virtual_mode", False):
            return original_resolve_path(self, key)

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
        _raise_if_symlink_loop(full)
        return full

    def _to_virtual_path_with_windows_compat(self: FilesystemBackend, path: Path) -> str:
        if not getattr(self, "virtual_mode", False):
            return original_to_virtual_path(self, path)

        full = normalize_windows_extended_prefix_path(path.resolve())
        cwd = normalize_windows_extended_prefix_path(self.cwd)
        return "/" + full.relative_to(cwd).as_posix()

    FilesystemBackend._resolve_path = _resolve_path_with_windows_compat  # type: ignore[method-assign]
    FilesystemBackend._to_virtual_path = _to_virtual_path_with_windows_compat  # type: ignore[method-assign]
    _WINDOWS_PATH_COMPAT_APPLIED = True


def _resolve_source_paths(paths: list[str], root: Path, skills_root: Path, *, default: list[str]) -> list[str]:
    candidates = paths if paths is not None else default
    resolved: list[str] = []
    for path in candidates:
        virtual_path = _to_virtual_source_path(path, root, skills_root)
        if not _virtual_path_exists(virtual_path, root, skills_root):
            continue
        if virtual_path not in resolved:
            resolved.append(virtual_path)
    return resolved


def _virtual_path_exists(virtual_path: str, root: Path, skills_root: Path) -> bool:
    if virtual_path == SKILLS_PREFIX or virtual_path.startswith(f"{SKILLS_PREFIX}/"):
        relative = virtual_path[len(SKILLS_PREFIX) :].lstrip("/")
        return (skills_root / relative).exists()
    if virtual_path == WORKSPACE_PREFIX or virtual_path.startswith(f"{WORKSPACE_PREFIX}/"):
        relative = virtual_path[len(WORKSPACE_PREFIX) :].lstrip("/")
        return (root / relative).exists()
    return False


def _to_virtual_source_path(path: str, root: Path, skills_root: Path) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    if raw == SKILLS_PREFIX or raw.startswith(f"{SKILLS_PREFIX}/"):
        return _normalize_virtual_path(raw)

    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        try:
            relative = candidate.resolve().relative_to(skills_root)
        except ValueError:
            return _to_virtual_workspace_path(path, root)
        relative_text = relative.as_posix()
        if not relative_text or relative_text == ".":
            return SKILLS_PREFIX
        return _normalize_virtual_path(f"{SKILLS_PREFIX}/{relative_text}")

    return _to_virtual_workspace_path(path, root)


def _to_virtual_workspace_path(path: str, root: Path) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        return WORKSPACE_PREFIX

    candidate = Path(raw).expanduser()
    try:
        relative = candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        if raw == WORKSPACE_PREFIX or raw.startswith(f"{WORKSPACE_PREFIX}/"):
            return _normalize_virtual_path(raw)
        if candidate.is_absolute():
            raise ValueError(f"Path `{path}` is outside workspace root `{root}`.") from exc
        relative = (root / candidate).resolve().relative_to(root.resolve())

    relative_text = relative.as_posix()
    if not relative_text or relative_text == ".":
        return WORKSPACE_PREFIX
    return _normalize_virtual_path(f"{WORKSPACE_PREFIX}/{relative_text}")


def _compiler_skills_root() -> Path:
    return Path(__file__).resolve().parents[2] / "skills"


def _normalize_virtual_path(path: str) -> str:
    normalized = "/" + str(path).strip().replace("\\", "/").strip("/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    if normalized != WORKSPACE_PREFIX and normalized.endswith("/"):
        return normalized.rstrip("/")
    return normalized


def _split_model_name(model: str) -> tuple[str, str]:
    if ":" not in model:
        return "", model.strip()
    provider, model_name = model.split(":", 1)
    return provider.strip().lower(), model_name.strip()


def _tool_name(tool: "BaseTool | dict[str, Any] | Any") -> str | None:
    if isinstance(tool, dict):
        name = tool.get("name")
        return name if isinstance(name, str) else None
    name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


def _normalize_tool_schema(tool: "BaseTool | dict[str, Any] | Any") -> "BaseTool | dict[str, Any] | Any":
    name = _tool_name(tool)
    if isinstance(tool, dict):
        copied = dict(tool)
        parameters = copied.get("parameters")
        if isinstance(parameters, dict):
            copied["parameters"] = _sanitize_json_schema(parameters)
        function = copied.get("function")
        if isinstance(function, dict) and isinstance(function.get("parameters"), dict):
            copied["function"] = {
                **function,
                "parameters": _sanitize_json_schema(function["parameters"]),
            }
        return copied

    schema = _openai_compatible_args_schema(tool, name)
    if schema is None:
        return tool
    if hasattr(tool, "model_copy"):
        return tool.model_copy(update={"args_schema": schema})
    return tool


def _openai_compatible_args_schema(tool: "BaseTool | Any", name: str | None) -> type[BaseModel] | None:
    if name == "glob":
        return OpenAIGlobSchema
    if name == "grep":
        return OpenAIGrepSchema

    args_schema = getattr(tool, "args_schema", None)
    if not isinstance(args_schema, type) or not issubclass(args_schema, BaseModel):
        return None
    raw_schema = args_schema.model_json_schema()
    if not _schema_needs_openai_normalization(raw_schema):
        return None
    return _build_openai_schema_model(name or "Tool", raw_schema)


def _schema_needs_openai_normalization(schema: dict[str, Any]) -> bool:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return False
    return any(isinstance(prop, dict) and "type" not in prop for prop in properties.values())


def _build_openai_schema_model(tool_name: str, schema: dict[str, Any]) -> type[BaseModel]:
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(schema.get("required") or [])
    fields: dict[str, tuple[Any, Any]] = {}
    for field_name, property_schema in properties.items():
        if not isinstance(field_name, str) or not isinstance(property_schema, dict):
            continue
        annotation = _annotation_from_json_schema(property_schema)
        default = ... if field_name in required else property_schema.get("default", None)
        description = property_schema.get("description")
        title = property_schema.get("title")
        fields[field_name] = (
            annotation,
            Field(default=default, description=description, title=title),
        )

    model_name = "".join(part for part in f"OpenAI{tool_name.title()}Schema" if part.isalnum())
    return create_model(model_name or "OpenAIToolSchema", __base__=BaseModel, **fields)


def _annotation_from_json_schema(schema: dict[str, Any]) -> Any:
    concrete = _first_non_null_schema(schema)
    schema_type = concrete.get("type")
    if schema_type == "string":
        return str
    if schema_type == "integer":
        return int
    if schema_type == "number":
        return float
    if schema_type == "boolean":
        return bool
    if schema_type == "array":
        item_annotation = _annotation_from_json_schema(concrete.get("items") or {})
        return list[item_annotation]
    if schema_type == "object":
        return dict[str, Any]
    return Any


def _first_non_null_schema(schema: dict[str, Any]) -> dict[str, Any]:
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        for candidate in any_of:
            if isinstance(candidate, dict) and candidate.get("type") != "null":
                return candidate
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        for candidate in one_of:
            if isinstance(candidate, dict) and candidate.get("type") != "null":
                return candidate
    return schema


def _sanitize_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    copied = dict(schema)
    copied.setdefault("type", "object")
    properties = copied.get("properties")
    if isinstance(properties, dict):
        copied["properties"] = {
            key: _sanitize_property_schema(value) if isinstance(value, dict) else value
            for key, value in properties.items()
        }
    return copied


def _sanitize_property_schema(schema: dict[str, Any]) -> dict[str, Any]:
    concrete = dict(_first_non_null_schema(schema))
    for key in ("default", "description", "title"):
        if key in schema and key not in concrete:
            concrete[key] = schema[key]
    if "type" not in concrete:
        concrete["type"] = "string"
    if concrete.get("type") == "object":
        return _sanitize_json_schema(concrete)
    if concrete.get("type") == "array" and isinstance(concrete.get("items"), dict):
        concrete["items"] = _sanitize_property_schema(concrete["items"])
    return concrete
