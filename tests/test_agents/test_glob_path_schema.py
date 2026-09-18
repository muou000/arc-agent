"""The glob tool schema must teach models to pass `path` explicitly.

deepagents' glob wrapper checks read permission against `/` when the model
omits `path` (``validate_path(path if path is not None else "/")``), and the
ARC permission set denies reads outside `/workspace` — so a pathless glob is
always rejected. Upstream's glob schema description says ``path`` "Defaults to
the backend's default root", which actively invites that doomed call: on the
2026-09-18 test1 run the InterfaceDesigner burned nine glob calls (four with
pathless patterns, five progressively simpler retries) before discovering
``path="/workspace"`` works. The schema sent to chat_completions models is
``OpenAIGlobSchema`` (swapped in by ``_openai_compatible_args_schema``), so its
``path`` description must say the argument is required in practice, and the
shared Tool Policy must repeat the rule where every stage agent reads it.
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from agents.context.prompts.common import workspace_tool_policy
from agents.runtime.factory import OpenAIGlobSchema, _normalize_tool_schema


class _UpstreamGlobSchema(BaseModel):
    """Mirror of deepagents' GlobSchema: the misleading upstream default."""

    pattern: str = ""
    path: str | None = None


def _glob_tool() -> StructuredTool:
    def glob(pattern: str, path: str | None = None) -> str:
        return ""

    return StructuredTool.from_function(
        name="glob",
        description="Find files matching a glob pattern.",
        func=glob,
        args_schema=_UpstreamGlobSchema,
    )


def test_glob_schema_path_description_says_path_is_required() -> None:
    description = OpenAIGlobSchema.model_fields["path"].description or ""
    assert "Required" in description or "required" in description
    assert "/workspace" in description
    # The misleading upstream default must not survive into the swapped schema.
    assert "default root" not in description


def test_normalized_glob_schema_reaches_the_model() -> None:
    normalized = _normalize_tool_schema(_glob_tool())
    schema = getattr(normalized, "args_schema", None)
    assert schema is OpenAIGlobSchema, "glob tool must be normalized to OpenAIGlobSchema"


def test_tool_policy_teaches_explicit_glob_path() -> None:
    policy = workspace_tool_policy()
    assert "explicit `path`" in policy
    assert "glob" in policy
