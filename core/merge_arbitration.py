"""LLM arbitration for merge-layer failures that mechanical resolution cannot fix.

Two trigger points in ``NodeWorktreeManager.integrate`` escalate here before
terminal failure:

1. **Non-additive conflicts.** The mechanical resolver (``_additive_merge_file``)
   only replays pure append-only insertions. When a side replaced or deleted
   lines - a genuine semantic conflict - the merge used to fail the node
   immediately. With arbitration enabled the merge state stays mid-merge and
   the arbiter receives the three-way diff of every conflicting file plus the
   interface contract cards of both sides, and may rewrite files *inside the
   conflict set only*. The result is then committed through the same
   post-merge health gate as an additive resolution.

2. **Health-gate failures.** An additively resolved merge that fails its
   post-merge health check (backend does not boot) used to abort
   unconditionally. With arbitration enabled the arbiter gets the failing
   files' context and one chance to repair the resolved tree; the gate then
   re-verifies before the merge commit.

Constraints (issue #81 / ADR 0001 leverage ③):

- The arbitration input is *pruned*: only the conflicting files' three-way
  diffs and both sides' contract cards. No repository-wide context is fed to
  the model - context pruning is the only cost gate.
- Narrow edit rights: writes are accepted only inside the conflict file set;
  any path outside it is rejected (one byte outside the set is one byte too
  many).
- A budget of exactly one arbitration per node (aligned with the DESIGN
  conflict requeue budget): a second trigger for the same node never calls
  the model again and goes straight to the existing requeue/terminal path.
- Gated by ``ARC_MERGE_ARBITRATION`` (default off). With the gate closed the
  merge layer behaves byte-for-byte like main.
- Fully auditable: every arbitration (input summary, model output, re-verify
  result) is appended to the runner events as ``merge_arbitration`` records.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from core.logging import append_debug_log, format_json_for_log
from core.scheduling_switches import ARC_MERGE_ARBITRATION as ARBITRATION_ENV


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]

# Env gate: default off; arbitration only runs with an explicit truthy value.
# ARBITRATION_ENV is imported from the authoritative registry
# (core/scheduling_switches.py) so a renamed/re-registered switch cannot
# drift between the read point and the test-isolation scrub list.

TRIGGER_CONFLICT = "conflict"
TRIGGER_HEALTH_GATE = "health-gate"

_ARBITRATION_DIRNAME = "merge-arbitration"


def arbitration_enabled() -> bool:
    """Whether merge-layer arbitration is enabled for this process.

    Default off. Like ``ARC_NODE_WORKTREES`` the value is read per call but
    expected to be stable within one run.
    """

    return os.environ.get(ARBITRATION_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ArbitrationInput:
    """The pruned context fed to the arbitration model.

    Holds exactly the two sanctioned inputs (issue #81): the conflicting
    files' three-way content and both sides' interface contract cards.
    ``files`` maps each conflict path to its base/ours/theirs stages; anything
    not reachable from here must never enter the model prompt.
    """

    trigger: str
    ours_label: str
    theirs_label: str
    # Conflict path -> {"base": str|None, "ours": str, "theirs": str} for the
    # conflict trigger; for the health-gate trigger (the mechanical resolution
    # already staged the files, so the merge index holds no stages) the map is
    # Conflict path -> {"resolved": str, "base": str|None, "ours": str|None,
    # "theirs": str|None} with the currently staged resolution in "resolved".
    files: dict[str, dict[str, str | None]] = field(default_factory=dict)
    # Node id -> contract card payload (the ``interfaces`` rows it owns).
    contract_cards: dict[str, Any] = field(default_factory=dict)
    # Health-gate failure detail (trigger == TRIGGER_HEALTH_GATE only).
    gate_failure: str = ""

    def conflict_paths(self) -> list[str]:
        return sorted(self.files)

    def build_prompt(self) -> str:
        lines: list[str] = []
        if self.trigger == TRIGGER_HEALTH_GATE:
            lines.append(
                "A git merge of two parallel feature branches was resolved "
                "mechanically (both sides' additions replayed), but the merged "
                "workspace fails its post-merge health gate."
            )
            lines.append(f"Health gate failure: {self.gate_failure or 'unknown'}")
        else:
            lines.append(
                "A git merge of two parallel feature branches has conflicting "
                "files that could not be resolved mechanically (at least one "
                "side modified or deleted existing lines)."
            )
        lines.append("")
        lines.append("## Conflict files")
        for path in self.conflict_paths():
            stages = self.files[path]
            lines.append(f"### {path}")
            if self.trigger == TRIGGER_HEALTH_GATE:
                lines.append("#### CURRENT RESOLVED CONTENT (mechanically merged; this is what fails)")
                lines.append("```")
                lines.append(str(stages.get("resolved") or ""))
                lines.append("```")
                for label, title in (
                    ("base", "BASE (common ancestor)"),
                    ("ours", f"OURS (integration branch, {self.ours_label})"),
                    ("theirs", f"THEIRS (incoming branch, {self.theirs_label})"),
                ):
                    if stages.get(label):
                        lines.append(f"#### {title}")
                        lines.append("```")
                        lines.append(str(stages.get(label)))
                        lines.append("```")
            else:
                lines.append("#### BASE (common ancestor)")
                lines.append("```")
                lines.append(str(stages.get("base") or ""))
                lines.append("```")
                lines.append(f"#### OURS (integration branch, {self.ours_label})")
                lines.append("```")
                lines.append(str(stages.get("ours") or ""))
                lines.append("```")
                lines.append(f"#### THEIRS (incoming branch, {self.theirs_label})")
                lines.append("```")
                lines.append(str(stages.get("theirs") or ""))
                lines.append("```")
            lines.append("")
        if self.contract_cards:
            lines.append("## Interface contract cards")
            for node_id, card in self.contract_cards.items():
                lines.append(f"### {node_id}")
                lines.append("```json")
                lines.append(format_json_for_log(card))
                lines.append("```")
                lines.append("")
        lines.append("## Task")
        lines.append(
            "Produce a single resolved version of every conflict file that "
            "preserves both sides' implemented behavior. The interfaces both "
            "sides declared (contract cards above) must all remain addressable. "
            "Reply with a JSON object mapping each conflict file path to its "
            "full resolved content. Only the listed conflict files may appear; "
            "any other path will be rejected."
        )
        return "\n".join(lines)


@dataclass
class ArbitrationResult:
    """Outcome of one arbitration attempt."""

    accepted: bool
    detail: str = ""
    # Path -> resolved content that was written back into the merge state.
    applied: dict[str, str] = field(default_factory=dict)


def merge_arbitration_budget_key() -> str:
    return "merge_arbitration_used"


class MergeArbiter:
    """One arbitration attempt over a mid-merge working tree.

    The arbiter never decides whether the merge ultimately lands: it only
    rewrites files inside the conflict set, and the caller's health gate
    re-verifies the tree before the merge commit. A rejected or failing
    arbitration leaves the merge state exactly as it was, so the existing
    abort/terminal path is unchanged.
    """

    def __init__(
        self,
        *,
        model: Any,
        workspace_path: str,
        log_cb: LogCallback | None = None,
        emit_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.model = model
        self.workspace_path = str(Path(workspace_path).resolve())
        self.log_cb = log_cb
        self.emit_event = emit_event

    async def arbitrate(
        self,
        handle_path: str,
        integration_branch: str,
        arbitration_input: ArbitrationInput,
        *,
        node_id: str = "",
        phase: str = "",
    ) -> ArbitrationResult:
        """Run one arbitration. Never raises for model/refusal failures.

        ``handle_path`` names the incoming node's worktree (for audit records
        only); edits apply to ``self.workspace_path``, which sits mid-merge
        with the conflict markers staged.
        """

        allowed = arbitration_input.conflict_paths()
        if not allowed:
            return ArbitrationResult(accepted=False, detail="no conflict files to arbitrate")
        record: dict[str, Any] = {
            "node_id": str(node_id or "").strip(),
            "phase": str(phase or "").strip(),
            "trigger": arbitration_input.trigger,
            "incoming_branch_worktree": str(handle_path),
            "integration_branch": integration_branch,
            "conflict_files": allowed,
        }
        try:
            message = arbitration_input.build_prompt()
            # Full input/output payloads go to the workspace debug log (the
            # runner event carries the compact summary); together they form
            # the audit trail.
            append_debug_log(
                "MergeArbiter",
                f"arbitration input ({arbitration_input.trigger}):\n{message}",
                node_id=node_id or None,
                workspace_root=self.workspace_path,
            )
            from langchain_core.messages import HumanMessage

            response = await self.model.ainvoke([HumanMessage(content=message)])
            append_debug_log(
                "MergeArbiter",
                f"arbitration output ({arbitration_input.trigger}):\n{_response_text(response) or ''}",
                node_id=node_id or None,
                workspace_root=self.workspace_path,
            )
            proposed = _extract_path_map(response)
            if proposed is None:
                record["proposed_paths"] = []
                detail = "arbitration model returned no parseable file map"
                record["outcome"] = "rejected"
                record["detail"] = detail
                self._emit(record)
                return ArbitrationResult(accepted=False, detail=detail)
            record["proposed_paths"] = sorted(proposed)
            outside = [path for path in proposed if path not in allowed]
            if outside:
                detail = (
                    "arbitration attempted to edit paths outside the conflict "
                    f"file set: {', '.join(outside[:8])}"
                )
                record["outcome"] = "rejected"
                record["detail"] = detail
                self._emit(record)
                return ArbitrationResult(accepted=False, detail=detail)
            missing = [path for path in allowed if path not in proposed]
            if missing:
                detail = (
                    "arbitration did not resolve every conflict file: missing "
                    f"{', '.join(missing[:8])}"
                )
                record["outcome"] = "rejected"
                record["detail"] = detail
                self._emit(record)
                return ArbitrationResult(accepted=False, detail=detail)
            for path in allowed:
                target = Path(self.workspace_path) / path
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "w", encoding="utf-8", newline="") as file:
                    file.write(proposed[path])
            record["outcome"] = "applied"
            record["detail"] = f"rewrote {len(allowed)} conflict file(s)"
            self._emit(record)
            await self._log(
                f"Merge arbitration ({arbitration_input.trigger}) rewrote "
                f"{len(allowed)} file(s): {', '.join(allowed[:8])}.",
                node_id=node_id,
            )
            return ArbitrationResult(
                accepted=True,
                detail=f"arbitration rewrote {len(allowed)} conflict file(s)",
                applied=dict(proposed),
            )
        except Exception as exc:  # noqa: BLE001 - one arbitration must never crash the merge
            detail = f"arbitration crashed: {type(exc).__name__}: {exc}"
            record["outcome"] = "crashed"
            record["detail"] = detail
            self._emit(record)
            return ArbitrationResult(accepted=False, detail=detail)

    def _emit(self, record: dict[str, Any]) -> None:
        if self.emit_event is None:
            return
        # The workflow's emitter routes the record through
        # ``EventClient.record_merge_arbitration``, which stamps the envelope
        # (``type`` + ``timestamp``); the record here is the compact summary.
        try:
            self.emit_event(record)
        except Exception:  # noqa: BLE001 - audit emission must not break the merge
            pass

    async def _log(self, message: str, *, node_id: str = "") -> None:
        if self.log_cb is None:
            return
        result = self.log_cb("MergeArbiter", message, "warning", node_id or None)
        if isinstance(result, Awaitable):
            await result


def _extract_path_map(response: Any) -> dict[str, str] | None:
    """Pull ``{path: content}`` out of a model response.

    Accepts either a plain JSON object (``{"/abs or rel path": "content"}``)
    or a fenced ```json block containing one. Returns ``None`` when nothing
    parseable is found.
    """

    import json
    import re

    text = _response_text(response)
    if text is None:
        return None
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, flags=re.DOTALL)
    if fence:
        candidates.append(fence.group(1).strip())
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict) and payload and all(
            isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
        ):
            return dict(payload)
    return None


def _response_text(response: Any) -> str | None:
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [part for part in content if isinstance(part, str)]
        if parts:
            return "\n".join(parts)
    return None


def read_workspace_file(workspace_path: str, path: str) -> str | None:
    """Read one workspace file for the health-gate arbitration input.

    The path is a conflict-set member (already validated by the caller), so a
    read failure surfaces as ``None`` rather than an exception.
    """

    try:
        with open(Path(workspace_path) / path, encoding="utf-8", newline="") as file:
            return file.read()
    except (OSError, UnicodeDecodeError):
        return None


def read_conflict_stages(
    git_runner: Callable[[list[str]], Any],
    paths: Sequence[str],
) -> dict[str, dict[str, str | None]]:
    """Read the three merge index stages for every conflict path.

    ``git_runner`` executes one git command (``["show", ":<stage>:<path>"]``)
    against the mid-merge workspace and returns a
    ``subprocess.CompletedProcess``-shaped object (``returncode``/``stdout``).
    Stage reads that fail (a path with no stage-1 base, e.g. add/add) surface
    as ``None`` content for that stage instead of aborting the read.
    """

    stages: dict[str, dict[str, str | None]] = {}
    for path in paths:
        entry: dict[str, str | None] = {}
        for stage, label in ((1, "base"), (2, "ours"), (3, "theirs")):
            result = git_runner(["show", f":{stage}:{path}"])
            if getattr(result, "returncode", 1) != 0:
                entry[label] = None
                continue
            raw = getattr(result, "stdout", None)
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError:
                    raw = raw.decode("utf-8", errors="replace")
            entry[label] = str(raw) if raw is not None else None
        stages[path] = entry
    return stages


def collect_contract_cards(
    traceability: Any,
    node_ids: Sequence[str],
) -> dict[str, Any]:
    """Both sides' interface contract cards, pruned to the conflicting nodes.

    The card is the node's ``interfaces`` rows (id, type, content, file_path)
    plus its ``node_contracts`` payload when present. Nodes with no stored
    cards are simply absent - an arbitration between two nodes whose designs
    never registered interfaces carries no card section at all.
    """

    cards: dict[str, Any] = {}
    for node_id in node_ids:
        card: dict[str, Any] = {}
        try:
            interfaces = traceability.list_interfaces(req_id=node_id)
        except Exception:  # noqa: BLE001 - a missing table must not break arbitration
            interfaces = []
        if interfaces:
            card["interfaces"] = [
                {
                    "interface_id": row.get("interface_id"),
                    "type": row.get("type"),
                    "content": row.get("content"),
                    "file_path": row.get("file_path"),
                }
                for row in interfaces
                if isinstance(row, dict)
            ]
        try:
            node_contract = traceability.get_node_contract(node_id)
        except Exception:  # noqa: BLE001
            node_contract = None
        if isinstance(node_contract, dict) and node_contract.get("content"):
            card["node_contract"] = node_contract.get("content")
        if card:
            cards[node_id] = card
    return cards
