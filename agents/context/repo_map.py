"""Live workspace map for stage-agent context.

The 12306 full-run metrics (2026-09-13/14) showed exploration-class tools
(``read_file`` discovery reads, ``ls``, ``grep``, ``glob``) at 89.3% of all
tool calls — 86 exploration calls versus 8 writes per task — because every
sibling node re-derived the same template layout. This module turns that
discovery into one deterministic scan: a bounded file inventory with exported
symbols, glue-anchor summaries for the shared integration points (route
registration, mounted endpoints, database tables), and per-file owning
requirement IDs from the traceability interfaces table.

Everything here is pure filesystem + regex work: the template is known, the
workspaces are small, and a full scan costs milliseconds, so the map is
simply rebuilt whenever its file-dependent context layer is invalidated.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - runtime duck-typing avoids the import cycle
    from app_type_handler.base import GlueAnchorSpec

# Directories that never carry hand-written source the agents must discover.
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".arc",
        ".git",
        ".cache",
        ".pytest_cache",
        ".workbuddy-ai",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "playwright-report",
        "test-results",
    }
)
# Files that exist in every workspace but carry no discovery value.
EXCLUDED_FILE_NAMES = frozenset({"package-lock.json", "yarn.lock", "pnpm-lock.yaml"})
# Extensions whose files get export-symbol extraction; everything else is
# listed as a bare path.
SOURCE_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py"})
# Skip export extraction for oversized files; they stay in the map as paths.
MAX_EXPORT_SCAN_BYTES = 128 * 1024

MAX_EXPORT_NAMES = 12
MAX_ANCHOR_TOKENS = 40
MAX_INVENTORY_LINES = 200

_ESM_EXPORT_RE = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:async\s+)?"
    r"(?:function\s*\*?\s*|const\s+|let\s+|class\s+|abstract\s+class\s+|interface\s+|type\s+|enum\s+)"
    r"([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
_ESM_DEFAULT_RE = re.compile(r"^\s*export\s+default\s+([A-Za-z_$][\w$]*)\s*[;\n]", re.MULTILINE)
_PY_DEF_RE = re.compile(r"^(?:def|class)\s+([A-Za-z_]\w*)", re.MULTILINE)

_REACT_ROUTE_RE = re.compile(
    r"<Route[^>]*\bpath=[\"']([^\"']+)[\"'][^>]*\belement=\{<([A-Za-z_$][\w$]*)"
)
_PAGE_IMPORT_RE = re.compile(r"^\s*import\s+[A-Za-z_$][\w$]*\s+from\s+['\"]([^'\"]+)['\"]", re.MULTILINE)
_EXPRESS_MOUNT_RE = re.compile(r"\bapp\.(?:use|get|post|put|delete|patch)\(\s*['\"]([^'\"]+)['\"]")
_EXPRESS_REQUIRE_RE = re.compile(r"^\s*const\s+[\w$]+\s*=\s*require\(['\"](\.[^'\"]*)['\"]\)", re.MULTILINE)
# The trailing ``(`` keeps prose matches out: init_db.js carries instruction
# comments like "Use CREATE TABLE IF NOT EXISTS to create new tables", where
# "to" is not a table name. Real DDL always opens its column list.
_SQL_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"']?([A-Za-z_]\w*)[`\"']?\s*\(", re.IGNORECASE
)


def _normalize_relative(path: str) -> str:
    return str(path or "").strip().replace("\\", "/").lstrip("/").removeprefix("./")


def _read_text_bounded(path: Path, limit: int = MAX_EXPORT_SCAN_BYTES) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as file:
            return file.read(limit)
    except OSError:
        return None


def _extract_exports(path: Path) -> list[str]:
    content = _read_text_bounded(path)
    if not content:
        return []
    names: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name and name not in seen and len(names) < MAX_EXPORT_NAMES:
            seen.add(name)
            names.append(name)

    if path.suffix == ".py":
        for match in _PY_DEF_RE.finditer(content):
            _add(match.group(1))
    else:
        for match in _ESM_EXPORT_RE.finditer(content):
            _add(match.group(1))
        for match in _ESM_DEFAULT_RE.finditer(content):
            _add(f"default:{match.group(1)}")
    return names


def _extract_anchor_tokens(content: str, extractor: str) -> list[str]:
    if not content:
        return []
    tokens: list[str] = []
    if extractor == "react_routes":
        tokens = [f"{match.group(1)} -> {match.group(2)}" for match in _REACT_ROUTE_RE.finditer(content)]
    elif extractor == "page_imports":
        tokens = [match.group(1) for match in _PAGE_IMPORT_RE.finditer(content)]
    elif extractor == "express_routes":
        tokens = [match.group(1) for match in _EXPRESS_MOUNT_RE.finditer(content)]
        tokens += [f"require:{match.group(1)}" for match in _EXPRESS_REQUIRE_RE.finditer(content)]
    elif extractor == "sql_tables":
        tokens = [match.group(1) for match in _SQL_TABLE_RE.finditer(content)]
    deduped: list[str] = []
    for token in tokens:
        if token not in deduped:
            deduped.append(token)
    return deduped[:MAX_ANCHOR_TOKENS]


def _anchor_lines(workspace: Path, specs: list[GlueAnchorSpec]) -> tuple[list[str], set[str]]:
    lines: list[str] = []
    anchor_paths: set[str] = set()
    for spec in specs:
        relative = _normalize_relative(spec.path)
        if not relative or ".." in relative.split("/"):
            continue
        anchor_paths.add(relative)
        path = workspace / relative
        if not path.is_file():
            continue
        # One bounded read per glue file, shared by all of its extractors.
        content = _read_text_bounded(path) or ""
        parts: list[str] = []
        for extractor in spec.extractors:
            tokens = _extract_anchor_tokens(content, extractor)
            if tokens:
                parts.append(f"{extractor}: {', '.join(tokens)}")
        summary = "; ".join(parts) if parts else "empty (no registered items yet)"
        lines.append(f"- glue {relative} ({spec.label}): {summary}")
    return lines, anchor_paths


def _owner_tag(relative: str, owners_by_path: dict[str, list[str]]) -> str:
    req_ids = owners_by_path.get(relative)
    if req_ids is None:
        # Interface records may carry virtual ``/workspace/...`` or ``./``
        # prefixed paths; fall back to suffix matching for those.
        for known, value in owners_by_path.items():
            normalized = _normalize_relative(known)
            if not normalized or normalized == relative:
                continue
            if relative.endswith(f"/{normalized}") or normalized.endswith(f"/{relative}"):
                req_ids = value
                break
    if not req_ids:
        return ""
    return f" [{', '.join(req_ids[:4])}]"


def build_workspace_map_lines(
    workspace: Path,
    anchor_specs: list[GlueAnchorSpec],
    owners_by_path: dict[str, list[str]] | None = None,
) -> list[str]:
    """Render the live workspace map as prompt lines.

    The map has three tiers: glue-anchor summaries (always per-file), the
    ordinary file inventory with exported symbols and owning requirement IDs,
    and, once the inventory outgrows ``MAX_INVENTORY_LINES``, per-directory
    rollups so the block stays bounded on mature workspaces.
    """

    if not workspace.is_dir():
        return []
    owners_by_path = owners_by_path or {}
    lines: list[str] = []

    anchor_lines, anchor_paths = _anchor_lines(workspace, anchor_specs)
    if anchor_lines:
        lines.append("Live workspace map — integration anchors (shared files every node must integrate through):")
        lines.extend(anchor_lines)

    entries: list[tuple[str, list[str]]] = []
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = sorted(name for name in dirnames if name not in EXCLUDED_DIR_NAMES)
        for filename in sorted(filenames):
            if filename in EXCLUDED_FILE_NAMES:
                continue
            path = Path(dirpath) / filename
            relative = _normalize_relative(str(path.relative_to(workspace)))
            if not relative or relative in anchor_paths:
                continue
            suffix = path.suffix.lower()
            exports = _extract_exports(path) if suffix in SOURCE_SUFFIXES else []
            entries.append((relative, exports))

    if not entries and not anchor_lines:
        return []

    if len(entries) <= MAX_INVENTORY_LINES:
        lines.append("Live workspace map — source files (path, exported symbols, owning requirement):")
        for relative, exports in entries:
            suffix = f" exports: {', '.join(exports)}" if exports else ""
            lines.append(f"- {relative}{suffix}{_owner_tag(relative, owners_by_path)}")
    else:
        # Mature workspace: keep per-file detail for owner-annotated files and
        # roll the anonymous remainder up per directory to bound the block.
        lines.append(
            "Live workspace map — files placed by earlier nodes (path, exported symbols, owning requirement):"
        )
        rolled: dict[str, int] = {}
        for relative, exports in entries:
            tag = _owner_tag(relative, owners_by_path)
            if tag:
                suffix = f" exports: {', '.join(exports)}" if exports else ""
                lines.append(f"- {relative}{suffix}{tag}")
            else:
                directory = relative.rsplit("/", 1)[0] if "/" in relative else "."
                rolled[directory] = rolled.get(directory, 0) + 1
        if rolled:
            lines.append("Other files (grouped by directory; open exact paths directly, do not list them):")
            for directory in sorted(rolled):
                lines.append(f"- {directory}/ ({rolled[directory]} files)")
    return lines
