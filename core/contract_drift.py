"""Contract drift validation at IMPLEMENT merge time (issue #83, ADR 0001 lever 2).

Under DESIGN gate pipelining a dependent starts designing while its
dependency is still implementing, so the registered interface card - not the
landed code - is the surface the dependent designs against. When the
dependency's IMPLEMENT merges, its landed tree must still honor the anchors
the card registered at DESIGN write time (``file_path`` plus the
``first_line`` anchor text). An implementation that moved the file or
reshaped the anchor silently invalidates every design already in flight
against that card; that is *drift*, and this module detects it mechanically.

The check is a guard, not a gate: consumers either escalate through the
merge-arbitration path (``ARC_MERGE_ARBITRATION`` on, one budget per node,
the #81 contract) or record a runner event and a warning and let downstream
TDD red lights be the final backstop. Nothing here raises on unreadable
state - a missing workspace reads as an empty tree and every anchored
contract reports drift, which is the conservative answer.

Anchors are compared the way the contract-skeleton registry builds them:
``file_path`` is workspace-relative, ``first_line`` is the anchor text the
DESIGN pass registered (a code line for FUNC/UI contracts, a line number for
DB table contracts). Only anchored rows can drift - a reused foreign row
without a file path carries no mechanical expectation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

REASON_ANCHOR_FILE_MISSING = "anchor-file-missing"
REASON_ANCHOR_LINE_MISSING = "anchor-line-missing"


@dataclass(frozen=True)
class ContractDrift:
    """One registered contract whose anchor is no longer findable in the tree."""

    interface_id: str
    file_path: str
    first_line: str
    reason: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "interface_id": self.interface_id,
            "file_path": self.file_path,
            "first_line": self.first_line,
            "reason": self.reason,
        }

    def describe(self) -> str:
        if self.reason == REASON_ANCHOR_FILE_MISSING:
            return (
                f"contract {self.interface_id}: registered anchor file "
                f"`{self.file_path}` is not in the merged tree (the implementation moved the surface)"
            )
        return (
            f"contract {self.interface_id}: anchor line {self.first_line!r} no longer "
            f"present in `{self.file_path}` (the implementation reshaped the surface)"
        )


def _normalize_path(raw: str) -> str:
    normalized = str(raw or "").strip().replace("\\", "/")
    if normalized.startswith("/workspace/"):
        normalized = normalized[len("/workspace/") :]
    return normalized.lstrip("/")


def _registered_anchor(contract: dict[str, Any]) -> tuple[str, str]:
    """(workspace-relative path, anchor text) of a registered contract.

    The anchor text lives either directly on the row (skeleton-derived
    ``first_line``) or inside the JSON ``content`` the DESIGN response
    stored; both spellings were written by the same registry, so both are
    accepted here. Rows without a path anchor return ``("", "")``.
    """

    file_path = _normalize_path(str(contract.get("file_path") or ""))
    if not file_path:
        return "", ""
    anchor = str(contract.get("first_line") or "").strip()
    if not anchor:
        content = contract.get("content")
        if isinstance(content, str) and content.strip():
            try:
                import json

                parsed = json.loads(content)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                anchor = str(parsed.get("first_line") or "").strip()
    return file_path, anchor


def detect_contract_drift(
    registered_contracts: list[dict[str, Any]],
    *,
    workspace_root: str,
) -> list[ContractDrift]:
    """Anchor-check every registered contract against the merged tree.

    ``registered_contracts`` are the node's interface rows as registered at
    DESIGN write time (the PR #64 registry's ground truth). A contract
    drifts when its anchor file is missing from ``workspace_root`` or the
    anchor line is no longer present in that file. Unreadable files count as
    missing: the check must never pass a contract it could not verify.
    """

    root = Path(workspace_root)
    drift: list[ContractDrift] = []
    for contract in registered_contracts:
        if not isinstance(contract, dict):
            continue
        file_path, anchor = _registered_anchor(contract)
        if not file_path:
            continue
        interface_id = str(contract.get("interface_id") or "").strip() or file_path
        landed = root / file_path
        try:
            if not landed.is_file():
                drift.append(
                    ContractDrift(
                        interface_id=interface_id,
                        file_path=file_path,
                        first_line=anchor,
                        reason=REASON_ANCHOR_FILE_MISSING,
                    )
                )
                continue
            text = landed.read_text(encoding="utf-8", errors="replace")
        except OSError:
            drift.append(
                ContractDrift(
                    interface_id=interface_id,
                    file_path=file_path,
                    first_line=anchor,
                    reason=REASON_ANCHOR_FILE_MISSING,
                )
            )
            continue
        if anchor and anchor not in text:
            drift.append(
                ContractDrift(
                    interface_id=interface_id,
                    file_path=file_path,
                    first_line=anchor,
                    reason=REASON_ANCHOR_LINE_MISSING,
                )
            )
    return drift
