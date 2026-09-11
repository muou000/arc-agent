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

import os
from typing import Any

_FALSE_VALUES = frozenset({"0", "false", "no", "off", "disabled"})

_checkpointer: Any | None = None


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

        _checkpointer = InMemorySaver()
    return _checkpointer


def reset_checkpointer() -> None:
    """Drop the shared saver so the next call builds a fresh one (tests)."""

    global _checkpointer
    _checkpointer = None
