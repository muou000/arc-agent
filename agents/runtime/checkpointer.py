"""Process-wide checkpoint store that lets ARC resume agent conversations.

ARC rebuilds each stage agent for every invocation, because the stage tools are
closures bound to a single requirement node (``run_tests``, the traceability
tools). Without a checkpointer every ``run()`` therefore starts a brand-new
conversation: the model re-reads its skill files, re-reads the source it already
read, and re-derives its plan from scratch.

LangGraph's checkpointer is keyed by ``thread_id``, and ARC already computes a
stable thread id per ``(node, phase, stage, test layer)``. Sharing one saver
across every rebuilt agent turns those repeated invocations into a *resumed*
conversation instead of a cold start. The TDD retry loop in
``core.phases.WorkflowPhaseRunner._run_tdd_for_node`` is the main beneficiary: it
re-invokes ``TestDrivenDeveloper`` up to ``TDD_RUN_TESTS_BUDGET`` times per layer,
and the post-run auto TDD retry re-invokes it again for every failing node.

The store is in-memory on purpose: it is scoped to one compilation process, and
the existing ``.arc/node_sessions/*.json`` + ``resume_context`` mechanism still
carries state across ``--resume`` process restarts. Set
``ARC_AGENT_CHECKPOINTER=0`` to disable reuse and restore cold-start behaviour.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

_FALSE_VALUES = frozenset({"0", "false", "no", "off", "disabled"})

# LangGraph's msgpack serializer warns for custom objects unless they are
# explicitly allowlisted. Keep this list narrow: these are the only Pydantic
# response objects ARC puts into stage-agent checkpoints.
_STAGE_RESPONSE_MSGPACK_ALLOWLIST = (
    ("agents.interface_designer", "InterfaceDesignResponse"),
    ("agents.test_generator", "TestGenerationResponse"),
)

_checkpointer: Any | None = None


def get_project_thread_namespace() -> str:
    """Return a stable short identifier for the active project.

    ARC's stage ``thread_id`` values are keyed only by ``(node, phase, stage)``.
    Because the checkpoint saver is process-wide, switching projects via
    ``configure_runtime`` would otherwise let a matching node/stage id resume
    another project's conversation. Prefixing every ``thread_id`` with this
    project-derived namespace keeps each project's checkpoints isolated while
    preserving stable ids within the same project.
    """

    from core.config import get_workspace_root

    root = get_workspace_root()
    return hashlib.sha256(root.encode("utf-8")).hexdigest()[:12]


def checkpointer_enabled() -> bool:
    """Return whether agent conversation reuse is enabled for this process."""

    raw = os.environ.get("ARC_AGENT_CHECKPOINTER", "1").strip().lower()
    return raw not in _FALSE_VALUES


def get_checkpointer() -> Any | None:
    """Return the shared checkpoint saver, creating it on first use.

    Returns ``None`` when reuse is disabled, which makes ``create_deep_agent``
    fall back to the previous cold-start behaviour.
    """

    global _checkpointer
    if not checkpointer_enabled():
        return None
    if _checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        _checkpointer = InMemorySaver(
            serde=JsonPlusSerializer(
                allowed_msgpack_modules=_STAGE_RESPONSE_MSGPACK_ALLOWLIST,
            )
        )
    return _checkpointer


def reset_checkpointer() -> None:
    """Drop the shared saver so the next call builds a fresh one (tests)."""

    global _checkpointer
    _checkpointer = None
