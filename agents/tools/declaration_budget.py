"""Consecutive-rejection escalation shared by the declaration tools.

``declare_test_manifest`` and ``declare_stage_write_set`` answer every
rejected call with the full rule text, which cannot by itself stop a model
that keeps re-sending the same payload: the easy-ticketbooking run of
2026-09-25 burned 60+ byte-identical manifest declarations (issue #113's
unwrap precedent saw 29 across 8 shapes) before something external ended
the stage. Once rejections stack up consecutively, the rejection grows an
explicit stop instruction so the model either fixes every listed issue in
one corrected re-declaration or ends the pass and lets the phase gate fail
bounded.

Advisory by design: the escalation hardens the message, never the
permission — a corrected re-declaration after the threshold still locks.
"""

from __future__ import annotations

#: Consecutive rejections after which the rejection text escalates. Each
#: rejection lists every issue, so legitimate iteration is a handful of
#: rounds; the #113 precedent showed a model needing 8+ shape guesses before
#: producing the right payload, so the bar sits above that while still
#: bounding a blind loop (the observed run averaged one call per ~3.5s).
ESCALATION_THRESHOLD = 8


def escalation_note(tool_name: str, rejections: int) -> str:
    """The stop instruction appended to a rejection past the threshold."""

    return (
        f" This tool has now rejected {rejections} consecutive declarations. "
        "Repeating the same or a trivially varied payload cannot succeed — the "
        "validation rules are mechanical. Fix every listed issue and re-declare "
        f"once, or stop calling `{tool_name}` and end the pass: the phase gate "
        "treats a missing declaration as a stage failure and escalates it, which "
        "is a bounded outcome, unlike more calls."
    )


def rejected_message(budget: "ConsecutiveRejectionBudget", tool_name: str, message: str) -> str:
    """Record one rejection and return the message, escalated past the threshold.

    The single wrap point both declaration tools route their errors through,
    so the escalation semantics stay identical across them.
    """

    count = budget.record_rejection()
    if count >= ESCALATION_THRESHOLD:
        return message + escalation_note(tool_name, count)
    return message


class ConsecutiveRejectionBudget:
    """Count consecutive rejections of one declaration tool.

    A locked declaration resets the count — a later re-declaration that only
    adds the paths of a previously failed entry starts a fresh budget instead
    of inheriting earlier churn.
    """

    def __init__(self, threshold: int = ESCALATION_THRESHOLD) -> None:
        self._threshold = threshold
        self._count = 0

    def record_rejection(self) -> int:
        """Record one rejection and return the consecutive count so far."""

        self._count += 1
        return self._count

    @property
    def escalated(self) -> bool:
        """Whether the consecutive count has reached the threshold."""

        return self._count >= self._threshold

    def record_acceptance(self) -> None:
        self._count = 0
