from __future__ import annotations

import time
from typing import Any

from .context import RuntimePaths
from .jsonio import append_jsonl


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _nonneg_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _nullable_nonneg_int(value: Any) -> int | None:
    """Normalize an optional breakdown: absent or invalid means "not reported"."""

    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _nonneg_float(value: Any) -> float | None:
    """Normalize an optional duration: absent or invalid means "not reported"."""

    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _normalized_transport(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"streamed", "plain"} else ""


def _positive_int(value: Any) -> int | None:
    """Normalize an attempt count: a call always has at least one attempt."""

    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1 else None


class EventClient:
    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        self._requirement_state_writer = None

    def set_requirement_state_writer(self, writer) -> None:
        self._requirement_state_writer = writer

    def _emit_requirement_state(self, node_id: str, phase: str, status: str, message: str | None = None) -> None:
        normalized_node_id = str(node_id or "").strip()
        if not normalized_node_id:
            return
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "requirement_state",
                "node_id": normalized_node_id,
                "phase": str(phase or "").strip(),
                "status": str(status or "").strip(),
                "timestamp": utc_timestamp(),
                "message": message,
            },
        )
        if self._requirement_state_writer is not None:
            state = {
                ("design", "running"): "DESIGNING",
                ("design", "completed"): "DESIGNED",
                ("design", "failed"): "FAILED",
                ("implement", "running"): "IMPLEMENTING",
                ("implement", "completed"): "IMPLEMENTED",
                ("implement", "failed"): "FAILED",
                ("test", "passed"): "PASSED",
                ("test", "failed"): "FAILED",
            }.get((str(phase or "").strip(), str(status or "").strip()))
            if state:
                self._requirement_state_writer(normalized_node_id, state, str(phase or "").strip())

    def mark_design_started(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "design", "running", message)

    def mark_design_done(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "design", "completed", message)

    def mark_design_failed(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "design", "failed", message)

    def mark_implementation_started(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "implement", "running", message)

    def mark_implementation_done(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "implement", "completed", message)

    def mark_implementation_failed(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "implement", "failed", message)

    def mark_test_passed(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "test", "passed", message)

    def mark_test_failed(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "test", "failed", message)

    def record_visual_analysis(
        self,
        *,
        node_id: str = "",
        status: str = "",
        attempt: int = 0,
        retry_at: str | None = None,
        message: str | None = None,
    ) -> None:
        """Append one node-level ``visual_analysis`` stage event.

        The event is deliberately separate from ``requirement_state``: visual
        analysis can be running or retrying while the aggregate DESIGN task is
        still pending. ``retry_at`` is an ISO timestamp when the stage is in
        backoff and otherwise remains null.
        """

        normalized_node_id = str(node_id or "").strip()
        if not normalized_node_id:
            return
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "visual_analysis",
                "node_id": normalized_node_id,
                "status": str(status or "").strip(),
                "attempt": _nonneg_int(attempt),
                "retry_at": str(retry_at or "").strip() or None,
                "message": message,
                "timestamp": utc_timestamp(),
            },
        )

    def record_llm_usage(
        self,
        *,
        node_id: str = "",
        phase: str = "",
        model: str = "",
        api_mode: str = "",
        source: str = "reported",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_write_1h_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        total_tokens: int = 0,
        cost: dict[str, Any] | None = None,
        duration_s: float | None = None,
        transport: str = "",
        attempts: int | None = None,
    ) -> None:
        """Append one ``llm_usage`` event for a single model call.

        Token semantics mirror the ARC-Bench reference (pi) ``Usage`` type:
        ``input`` excludes cache reads/writes (they are separate fields and a
        subset of the provider prompt total), ``reasoning`` is a subset of
        ``output``. ``source`` distinguishes provider-reported usage from a
        local token estimate. An empty ``node_id`` attributes the call to the
        run as a whole (model calls made outside any node's context).

        ``latency`` carries the optional call telemetry: end-to-end
        ``duration_s``, the HTTP ``transport`` that produced the result
        (``streamed`` | ``plain``; empty when unknown) and the ``attempts``
        count spent by the adapter retry loop. All three stay ``None``/empty
        for events written by older callers, so readers must treat them as
        optional.
        """
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "llm_usage",
                "node_id": str(node_id or "").strip(),
                "phase": str(phase or "").strip(),
                "model": str(model or "").strip(),
                "api_mode": str(api_mode or "").strip(),
                "source": str(source or "").strip() or "reported",
                "usage": {
                    "input": _nonneg_int(input_tokens),
                    "output": _nonneg_int(output_tokens),
                    "cache_read": _nonneg_int(cache_read_tokens),
                    "cache_write": _nonneg_int(cache_write_tokens),
                    "cache_write_1h": _nullable_nonneg_int(cache_write_1h_tokens),
                    "reasoning": _nullable_nonneg_int(reasoning_tokens),
                    "total": _nonneg_int(total_tokens),
                },
                "latency": {
                    "duration_s": _nonneg_float(duration_s),
                    "transport": _normalized_transport(transport),
                    "attempts": _positive_int(attempts),
                },
                "cost": cost if isinstance(cost, dict) else None,
                "timestamp": utc_timestamp(),
            },
        )

    def record_tool_usage(
        self,
        *,
        node_id: str = "",
        phase: str = "",
        tool: str = "",
        status: str = "ok",
        path: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
        result_chars: int = 0,
    ) -> None:
        """Append one ``tool_usage`` event for a single agent tool round-trip.

        ``status`` is ``"ok"``, ``"error"`` (the tool ran and failed) or
        ``"blocked"`` (a discipline middleware refused the call before
        execution). ``detail`` carries the per-tool observations used to spot
        wasteful round-trips: file reads record their ``offset``/``limit``
        (``limit=None`` means the model asked for an unpaged, whole-file read)
        and every event records the result size so empty grep/read results are
        visible. An empty ``node_id`` attributes the call to the run as a whole.
        """
        normalized_chars = _nonneg_int(result_chars)
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "tool_usage",
                "node_id": str(node_id or "").strip(),
                "phase": str(phase or "").strip(),
                "tool": str(tool or "").strip(),
                "status": str(status or "").strip() or "ok",
                "detail": {
                    "path": str(path or "").strip() or None,
                    "offset": _nullable_nonneg_int(offset),
                    "limit": _nullable_nonneg_int(limit),
                    "result_chars": normalized_chars,
                    "result_empty": normalized_chars == 0,
                },
                "timestamp": utc_timestamp(),
            },
        )

    def record_layer_reverify(
        self,
        *,
        node_id: str = "",
        layer: str = "",
        status: str = "",
        files: list[str] | None = None,
        used: int = 0,
        message: str | None = None,
    ) -> None:
        """Append one ``layer_reverify`` event for the TDD late-fix channel.

        Emitted twice per re-verification: once with ``status="triggered"``
        when the system re-runs a budget-exhausted layer's manifest files (a
        later layer already passed, so the fix may have landed late), and
        once with ``status="passed"``/``"failed"`` carrying the outcome.
        ``used`` is the ``run_tests`` budget the layer had already spent when
        the re-verification fired; ``files`` records the manifest-scoped file
        list that was executed. An empty ``node_id`` attributes the event to
        the run as a whole.
        """
        normalized_files = [str(path or "").strip() for path in (files or [])]
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "layer_reverify",
                "node_id": str(node_id or "").strip(),
                "layer": str(layer or "").strip(),
                "status": str(status or "").strip(),
                "files": [path for path in normalized_files if path],
                "used": _nonneg_int(used),
                "message": message,
                "timestamp": utc_timestamp(),
            },
        )

    def record_tdd_stall(
        self,
        *,
        node_id: str = "",
        layer: str = "",
        reason: str = "",
        fingerprint: str = "",
        repetitions: int = 0,
        threshold: int = 0,
        used: int = 0,
        budget: int = 0,
        suggested_action: str = "",
    ) -> None:
        """Append deterministic repeated-fingerprint stop evidence (issue #264)."""

        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "tdd_stall",
                "node_id": str(node_id or "").strip(),
                "layer": str(layer or "").strip(),
                "reason": str(reason or "").strip(),
                "fingerprint": str(fingerprint or "").strip(),
                "repetitions": _nonneg_int(repetitions),
                "threshold": _nonneg_int(threshold),
                "used": _nonneg_int(used),
                "budget": _nonneg_int(budget),
                "suggested_action": str(suggested_action or "").strip(),
                "timestamp": utc_timestamp(),
            },
        )

    def record_test_contract_preflight(
        self,
        *,
        node_id: str = "",
        status: str = "",
        classification: str = "",
        files: list[str] | None = None,
        issues: list[dict[str, Any]] | None = None,
        message: str | None = None,
    ) -> None:
        """Append the static test-contract gate result for one node."""

        normalized_node_id = str(node_id or "").strip()
        if not normalized_node_id:
            return
        normalized_files = [str(path or "").strip() for path in (files or [])]
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "test_contract_preflight",
                "node_id": normalized_node_id,
                "status": str(status or "").strip(),
                "classification": str(classification or "").strip(),
                "files": [path for path in normalized_files if path],
                "issues": [issue for issue in (issues or []) if isinstance(issue, dict)],
                "message": message,
                "timestamp": utc_timestamp(),
            },
        )

    def record_rebase_replay(
        self,
        *,
        node_id: str = "",
        status: str = "",
        files: list[str] | None = None,
        message: str | None = None,
    ) -> None:
        """Append one ``rebase_replay`` event for the mid-phase replay.

        Emitted at the replay lifecycle's decision points (issue #127 /
        ADR 0003): ``replayed`` for a clean replay, ``conflicts`` when the
        rebase landed with conflict markers for the resolving agent,
        ``aborted`` when a mechanical failure rolled the worktree back
        (fail-open), and ``skipped`` when a boundary check consumed the
        pending set without a replay. ``files`` carries the applied or
        conflicted paths. An empty ``node_id`` attributes the event to the
        run as a whole.
        """
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "rebase_replay",
                "node_id": str(node_id or "").strip(),
                "status": str(status or "").strip(),
                "files": [str(path or "").strip() for path in (files or []) if str(path or "").strip()],
                "message": message,
                "timestamp": utc_timestamp(),
            },
        )

    def record_stray_sweep(
        self,
        *,
        node_id: str = "",
        files: list[str] | None = None,
        message: str | None = None,
    ) -> None:
        """Append one ``stray_sweep`` event for the IMPLEMENT wrap-up cleanup.

        Emitted when the sweep deletes stray workspace files (issue #159):
        paths outside every template skeleton root whose content duplicates a
        committed in-skeleton file. ``files`` carries the deleted
        workspace-relative paths. An empty ``node_id`` attributes the event
        to the run as a whole.
        """
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "stray_sweep",
                "node_id": str(node_id or "").strip(),
                "files": [str(path or "").strip() for path in (files or []) if str(path or "").strip()],
                "message": message,
                "timestamp": utc_timestamp(),
            },
        )

    def record_zero_test_leaf(
        self,
        *,
        node_id: str = "",
        interface_count: int = 0,
        summary: str | None = None,
    ) -> None:
        """Append one ``zero_test_leaf`` event for the zero-test leaf observation.

        Emitted when a leaf node that owns interface contracts registered an
        empty test manifest, so IMPLEMENT skips TDD and marks the interfaces
        implemented directly (issue #187). Observation-only: an empty manifest
        stays a legal DESIGN result — no gate, no retry — until run data says
        otherwise. ``interface_count`` counts the node's owned interfaces;
        ``summary`` quotes the TestGenerator's own reason text when it supplied
        one. An empty ``node_id`` attributes the event to the run as a whole.
        """
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "zero_test_leaf",
                "node_id": str(node_id or "").strip(),
                "interface_count": _nonneg_int(interface_count),
                "summary": summary,
                "timestamp": utc_timestamp(),
            },
        )

    def record_design_convergence(
        self,
        *,
        node_id: str = "",
        status: str = "reused",
        interface_ids: list[Any] | None = None,
        interface_status: dict[str, Any] | None = None,
        test_ids: list[Any] | None = None,
        coverage: list[dict[str, Any]] | None = None,
        checkpoint_ids: list[Any] | None = None,
        checkpoint_paths: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> None:
        """Append auditable evidence for a DESIGN reuse/convergence decision.

        This event is emitted only after the deterministic green-baseline
        reuse gate has verified interface status, complete manifest coverage,
        implementation checkpoints, and current passing test files.  The
        event complements the persisted node session and traceability rows so
        resume/retry consumers can distinguish reuse from an ordinary RED
        witness path.
        """

        normalized_status = str(status or "reused").strip().lower()
        if normalized_status not in {"reused", "converged"}:
            normalized_status = "reused"

        def normalized_strings(values: list[Any] | None) -> list[str]:
            return [str(value or "").strip() for value in (values or []) if str(value or "").strip()]

        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "design_convergence",
                "node_id": str(node_id or "").strip(),
                "status": normalized_status,
                "result_state": "CONVERGED",
                "interface_ids": normalized_strings(interface_ids),
                "interface_status": {
                    str(interface_id or "").strip(): str(state or "").strip()
                    for interface_id, state in (interface_status or {}).items()
                    if str(interface_id or "").strip() and str(state or "").strip()
                },
                "test_ids": normalized_strings(test_ids),
                "coverage": list(coverage or []),
                "checkpoint_ids": normalized_strings(checkpoint_ids),
                "checkpoint_paths": {
                    str(interface_id or "").strip(): str(path or "").strip()
                    for interface_id, path in (checkpoint_paths or {}).items()
                    if str(interface_id or "").strip() and str(path or "").strip()
                },
                "message": message,
                "timestamp": utc_timestamp(),
            },
        )

    def record_edge_reconcile(
        self,
        *,
        backfilled: list[dict[str, Any]] | None = None,
        unresolved: list[dict[str, Any]] | None = None,
        message: str | None = None,
    ) -> None:
        """Append one ``edge_reconcile`` event for the compile-wrap-up sweep.

        Emitted at the completion point after the queue drains (issue #238):
        the sweep re-derives every stored interface's callers/callees against
        the final store state, so ``backfilled`` carries the cross_req edges
        forward references left missing (both endpoints registered, edge
        absent) and ``unresolved`` the references that still resolved to no
        stored contract at compile end. The event is what makes the
        backfill — which registration-time warnings cannot show — auditable
        alongside the traceability tables it repaired.
        """
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "edge_reconcile",
                "backfilled": list(backfilled or []),
                "unresolved": list(unresolved or []),
                "message": message,
                "timestamp": utc_timestamp(),
            },
        )

    def record_traceability_row_event(self, payload: dict[str, Any]) -> None:
        """Append one pre-shaped traceability row event (``interface_upsert`` /
        ``interface_status`` / ``test_upsert``).

        The row events mirror the persisted traceability row field-for-field
        and are emitted by :class:`TraceabilityStore` right after the row
        lands. The payload is the store's own normalized row projection plus
        its ``type`` discriminator; this method only stamps the timestamp, so
        the store never formats runner-event envelopes itself.
        """
        self._emit_traceability_event(payload)

    def record_git_identity_configured(self, user_name: str, user_email: str) -> None:
        """Append the ``git_identity_configured`` signal from ``GitClient``.

        A commit-history refresh signal carrying the configured identity in
        ``message``; emitted once per ``ensure_repo``.
        """
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "signal",
                "reason": "git_identity_configured",
                "refresh": {
                    "submission": False,
                    "logs": False,
                    "commit_history": True,
                    "traceability_selected": False,
                    "traceability_all": False,
                    "preview": False,
                },
                "message": f"{user_name} <{user_email}>",
                "timestamp": utc_timestamp(),
            },
        )

    def record_contract_drift(
        self,
        *,
        node_id: str = "",
        drift: list[dict[str, Any]] | None = None,
        arbitration: bool = False,
        outcome: str | None = None,
    ) -> None:
        """Append one ``contract_drift`` event (DESIGN gate pipelining).

        Emitted when a merged IMPLEMENT no longer honors the anchors its
        DESIGN registered: once with ``arbitration`` naming whether the
        escalation path is enabled, and - after a successful arbitration
        repair - once more with ``outcome="repaired"``. ``drift`` carries the
        per-anchor drift payloads. An empty ``node_id`` attributes the event
        to the run as a whole.
        """
        payload: dict[str, Any] = {
            # Field order matches the pre-refactor emitter exactly: the
            # timestamp leads, the discriminator and fields follow, and the
            # optional outcome trails (ticket #163 pins the order).
            "timestamp": utc_timestamp(),
            "type": "contract_drift",
            "node_id": str(node_id or "").strip(),
            "drift": drift if drift is not None else [],
            "arbitration": bool(arbitration),
        }
        if outcome is not None:
            payload["outcome"] = outcome
        append_jsonl(self.paths.runner_events_path, payload)

    def record_merge_arbitration(
        self, record: dict[str, Any], *, timestamp: bool = True
    ) -> None:
        """Append one ``merge_arbitration`` audit record.

        ``record`` is the arbiter's compact summary (node/phase/trigger/
        outcome/detail and friends) without the envelope; this method stamps
        the ``type`` discriminator and - by default - the timestamp, in that
        order, so the audit stream's field order stays stable for the
        frontend reader.

        The workflow's post-reverify audit path predates the envelope
        stamping and historically emitted without a timestamp; byte
        compatibility pins that shape (issue #163), so that caller passes
        ``timestamp=False``.
        """
        payload: dict[str, Any] = {"type": "merge_arbitration"}
        if timestamp:
            payload["timestamp"] = utc_timestamp()
        payload.update(record)
        append_jsonl(self.paths.runner_events_path, payload)

    def _emit_runner_state(self, state: str, message: str | None = None) -> None:
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "runner_state",
                "state": str(state or "").strip(),
                "timestamp": utc_timestamp(),
                "message": message,
            },
        )

    def mark_run_started(self, message: str | None = None) -> None:
        self._emit_runner_state("running", message)

    def mark_run_completed(self, message: str | None = None) -> None:
        self._emit_runner_state("completed", message)

    def mark_run_failed(self, message: str | None = None) -> None:
        self._emit_runner_state("failed", message)

    def mark_run_paused(self, message: str | None = None) -> None:
        self._emit_runner_state("paused", message)

    def mark_run_resumed(self, message: str | None = None) -> None:
        self._emit_runner_state("resumed", message)

    def _emit_traceability_event(self, payload: dict[str, Any]) -> None:
        normalized = dict(payload)
        normalized.setdefault("timestamp", utc_timestamp())
        append_jsonl(self.paths.runner_events_path, normalized)

    def _emit_refresh_signal(
        self,
        *,
        reason: str,
        submission: bool = False,
        logs: bool = False,
        commit_history: bool = False,
        traceability_selected: bool = False,
        traceability_all: bool = False,
        preview: bool = False,
    ) -> None:
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "signal",
                "reason": str(reason or "").strip() or "arcbench_agent_runtime",
                "timestamp": utc_timestamp(),
                "refresh": {
                    "submission": bool(submission),
                    "logs": bool(logs),
                    "commit_history": bool(commit_history),
                    "traceability_selected": bool(traceability_selected),
                    "traceability_all": bool(traceability_all),
                    "preview": bool(preview),
                },
            },
        )

    def notify_traceability_changed(self, reason: str) -> None:
        self._emit_refresh_signal(
            reason=reason,
            submission=True,
            traceability_selected=True,
            traceability_all=True,
        )

    def notify_commit_history_changed(self, reason: str, *, preview: bool = False) -> None:
        self._emit_refresh_signal(
            reason=reason,
            commit_history=True,
            preview=preview,
        )

    def read_demo_test_status_payload(self) -> dict[str, Any]:
        return {"tests": {}, "requirements": {}}

    def write_demo_test_status_payload(self, payload: dict[str, Any]) -> None:
        return None

    def set_demo_test_status(self, test_id: str, status: str | None) -> None:
        normalized_test_id = str(test_id or "").strip()
        if not normalized_test_id:
            return
        self.notify_traceability_changed("demo_test_status_updated")

    def set_demo_test_statuses(self, status_by_test_id: dict[str, str | None]) -> None:
        if not status_by_test_id:
            return
        self.notify_traceability_changed("demo_test_statuses_updated")

    def clear_demo_test_statuses(self, test_ids: list[str]) -> None:
        if not test_ids:
            return
        self.notify_traceability_changed("demo_test_statuses_cleared")

    def set_demo_requirement_status(self, req_id: str, status: str | None) -> None:
        normalized_req_id = str(req_id or "").strip()
        if not normalized_req_id:
            return
        self.notify_traceability_changed("demo_requirement_status_updated")
