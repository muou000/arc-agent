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
