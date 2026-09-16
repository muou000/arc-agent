"""Deterministic contract skeletons derived from materialized design files.

Flash-class models write the design files but fail to serialize the matching
``interfaces`` array in one large structured response (observed 2026-09-16 on
the ticket-booking benchmark: every DESIGN pass with 9-12 materialized files
returned ``"interfaces": []``). The contract identities, however, are fully
determined by the files themselves: an express router file is an API contract,
a service module is a FUNC contract, a ``CREATE TABLE`` statement is a DB
contract, a page component is a UI contract.

This module turns the discipline's materialized-path list into contract
skeleton records so the repair path can degrade the "one large free-form
structured output" into "fill in two semantic fields per pre-computed row".
Everything here is mechanical file inspection: names, line anchors, routes and
table names come from parsing the actual file content, so no invented paths,
exports or tables can enter the traceability store through this channel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

INTERFACE_TYPES = frozenset({"UI", "API", "FUNC", "DB"})

# Shared integration surfaces of the web template: edits here extend already
# registered contracts (parent shell, app entry, route registration, database
# bootstrap/seed), so they map to update relations instead of new contracts.
# The frontend/backend split of the web template is the deployment convention
# the file scanner keys on; a repo without those roots yields no skeletons.
_BACKEND_ROOT_PARTS = ("backend", "server")
_FRONTEND_ROOT_PARTS = ("frontend", "client", "web")

_SHARED_FILE_NAMES = frozenset(
    {
        "app.js",
        "app.ts",
        "app.tsx",
        "index.js",
        "index.ts",
        "init_db.js",
        "init_db.ts",
        "seed_db.js",
        "seed_db.ts",
        "db_runtime.js",
        "db_runtime.ts",
    }
)

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?([A-Za-z_][A-Za-z0-9_]*)[\"'`\]]?",
    re.IGNORECASE,
)
_EXPRESS_MOUNT_RE = re.compile(r"app\.use\(\s*[\"'`]([^\"'`]+)[\"'`]")
_ROUTER_METHOD_RE = re.compile(r"router\.(get|post|put|patch|delete)\(\s*[\"'`]([^\"'`]+)[\"'`]")
# Export targets: `export function X`, `export default function X`,
# `export const X`, `module.exports = { X, Y }`, `module.exports = X`.
# DOCX-style comment badges (`/** REQ-2 ... */`) and doc comments are skipped:
# the name must come from real code, never from a doc line.
_EXPORT_RE = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|const|class)\s+([A-Za-z_$][\w$]*)"
    r"|^\s*export\s+const\s+([A-Za-z_$][\w$]*)"
    r"|^\s*module\.exports\s*=\s*\{([^}]*)\}"
    r"|^\s*module\.exports\s*=\s*([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
# A router/service/repository module whose header doc comment mentions a
# `REQ-n ...` badge is the node-owned boundary; the module stem is the name
# unless the badge itself names the boundary (e.g. "REQ-2 authentication
# domain logic" -> "auth"). Used only when no export list exists.
_REQ_BADGE_RE = re.compile(r"/\*\*?\s*(?:\*\s*)*REQ-[A-Za-z0-9_-]+\s+(.{3,60}?)[.*\s]*\*/", re.DOTALL)


def _code_lines(content: str) -> list[str]:
    """Drop comment text so names and anchors come from real code only.

    Handles single-line block comments with trailing code
    (``/* header */ const a = 1;``) and multi-line blocks whose closing line
    carries code after ``*/``; the code tail is kept and comment tails after
    ``//`` are left alone (they are part of the code line's own text).
    """

    kept: list[str] = []
    in_block = False
    for line in content.split("\n"):
        stripped = line.strip()
        if in_block:
            if "*/" in stripped:
                in_block = False
                after_close = stripped.split("*/", 1)[1].strip()
                if after_close and not _is_comment_text(after_close):
                    kept.append(after_close)
            continue
        if stripped.startswith("/*"):
            if "*/" in stripped:
                # Single-line block comment; keep any code after the closer.
                after_close = stripped.split("*/", 1)[1].strip()
                if after_close and not _is_comment_text(after_close):
                    kept.append(after_close)
            else:
                in_block = True
            continue
        if _is_comment_text(stripped):
            continue
        kept.append(line.rstrip())
    return kept


def _is_comment_text(text: str) -> bool:
    return text.startswith("//") or text.startswith("*") or text.startswith("/*")


@dataclass
class ContractSkeleton:
    """One mechanically derived interface-contract row awaiting semantics."""

    interface_id: str
    req_id: str
    file_path: str
    first_line: str
    type: str
    name: str
    relation: str = "owned"
    method: str | None = None
    mount_path: str | None = None
    table_name: str | None = None
    export_names: list[str] = field(default_factory=list)

    def to_prompt_row(self) -> str:
        """One line of the fill-in list handed to the model."""

        location = self.file_path
        if self.method and self.mount_path:
            location += f" ({self.method.upper()} {self.mount_path})"
        elif self.table_name:
            location += f" (CREATE TABLE {self.table_name})"
        return f"- {self.interface_id} [{self.type}] {location} :: first_line={self.first_line!r}"


def _strip_workspace_prefix(path: str) -> str:
    normalized = str(path or "").strip().replace("\\", "/")
    if normalized.startswith("/workspace/"):
        return normalized[len("/workspace/") :]
    return normalized.lstrip("/")


def _is_shared_file(rel_path: str) -> bool:
    name = rel_path.rsplit("/", 1)[-1].lower()
    if name in _SHARED_FILE_NAMES:
        return True
    return name in {"app.jsx"} or name.endswith(".d.ts")


def _classify_dir(rel_path: str) -> str | None:
    lower = rel_path.lower()
    parts = rel_path.split("/")
    root = parts[0].lower() if parts else ""

    def _contains(segment: str) -> bool:
        return f"/{segment}/" in f"/{lower}/"

    if root in _BACKEND_ROOT_PARTS:
        if _contains("routes") or _contains("routers"):
            return "API"
        if any(_contains(seg) for seg in ("services", "repositories", "repo")):
            return "FUNC"
        return "FUNC"
    if root in _FRONTEND_ROOT_PARTS:
        # Frontend api-client modules (`frontend/src/api/...`,
        # `frontend/src/features/auth/authApi.ts`) are FUNC boundaries even
        # though they live under the frontend tree.
        if _contains("api") or _is_api_client_name(parts[-1]):
            return "FUNC"
        return "UI"
    if any(_contains(seg) for seg in ("routes", "routers")):
        return "API"
    if any(_contains(seg) for seg in ("services", "repositories")):
        return "FUNC"
    if _contains("api") or _is_api_client_name(parts[-1]):
        return "FUNC"
    if any(_contains(seg) for seg in ("pages", "views", "components", "features")):
        return "UI"
    return None


def _is_api_client_name(file_name: str) -> bool:
    stem = file_name.rsplit(".", 1)[0].lower()
    return stem in {"api", "apiclient", "http", "httpclient", "request", "requests"} or stem.endswith("api")


def _skeleton_name(rel_path: str, stem: str, suffix: str, type_hint: str | None) -> str:
    if type_hint == "API":
        # Real-run convention: `auth_routes.js` -> `AuthRoutes` (the whole
        # stem, pascal-cased — no suffix stripping; run5's qwen contract for
        # this exact file shape was `REQ-1-API-AuthRoutes`).
        return _to_pascal_case(stem)
    if type_hint == "DB":
        return _to_pascal_case(f"{stem}_schema") if "schema" not in stem else _to_pascal_case(stem)
    if type_hint == "UI":
        return _to_pascal_case(stem)
    if suffix == "py":
        return _to_snake_case(stem)
    return _to_pascal_case(stem)


def _to_pascal_case(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", " ", str(text or "").strip())
    return "".join(part[:1].upper() + part[1:] for part in cleaned.split() if part) or "Contract"


def _to_snake_case(text: str) -> str:
    cleaned = re.sub(r"([^A-Z])([A-Z])", r"\1_\2", str(text or "").strip())
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", cleaned).strip("_").lower()
    return cleaned or "contract"


def _first_meaningful_line(lines: list[str]) -> str:
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*") or stripped.startswith("#"):
            continue
        return stripped
    return ""


def _line_number_of(lines: list[str], text: str) -> str:
    for index, line in enumerate(lines, start=1):
        if text and text in line:
            return str(index)
    return "1"


def _export_names(content: str) -> list[str]:
    names: list[str] = []
    for match in _EXPORT_RE.finditer(content):
        for group in match.groups():
            if not group:
                continue
            if "," in group:
                names.extend(part.split(":")[0].strip() for part in group.split(",") if part.split(":")[0].strip())
            else:
                name = group.strip()
                # `module.exports = seedDatabase;` style single-name exports
                # (name repeated via aliases such as `module.exports.seed`) —
                # keep the first occurrence only.
                names.append(name)
    return list(dict.fromkeys(names))[:12]


def _table_names_in_code(content: str) -> list[str]:
    """CREATE TABLE statements that appear outside comments, in file order."""

    tables: list[str] = []
    for match in _CREATE_TABLE_RE.finditer(content):
        line_start = content.rfind("\n", 0, match.start()) + 1
        line_end = content.find("\n", match.end())
        line = content[line_start : line_end if line_end != -1 else len(content)]
        stripped = line.strip()
        if stripped.startswith("*") or stripped.startswith("//") or stripped.startswith("/*"):
            continue
        tables.append(match.group(1))
    return tables


def derive_contract_skeletons(
    *,
    node_id: str,
    file_paths: list[str],
    workspace_root: str,
    interface_ids_by_file: dict[str, set[str]] | None = None,
) -> list[ContractSkeleton]:
    """Derive contract skeletons from files materialized by the design pass.

    ``file_paths`` are the discipline's materialized paths (virtual
    ``/workspace/...`` or workspace-relative). Every skeleton is anchored to a
    real file inside ``workspace_root``; a missing or unreadable file yields no
    skeleton. Files on shared integration surfaces whose edits extend already
    registered contracts (``interface_ids_by_file`` keyed by normalized
    workspace-relative path) are marked as updates of those contracts instead
    of minting new ones.
    """

    claims = interface_ids_by_file or {}
    skeletons: list[ContractSkeleton] = []
    root = Path(workspace_root).resolve()
    seen: set[str] = set()

    for raw_path in file_paths:
        rel_path = _strip_workspace_prefix(raw_path)
        if not rel_path or rel_path in seen:
            continue
        seen.add(rel_path)

        candidate = (root / rel_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if not candidate.is_file():
            continue
        try:
            content = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        code = _code_lines(content)
        lines = content.split("\n")
        stem = candidate.stem
        suffix = candidate.suffix.lstrip(".").lower()

        existing_ids = claims.get(rel_path) or claims.get(f"/{rel_path}") or set()
        if existing_ids:
            for interface_id in sorted(existing_ids):
                skeletons.append(
                    ContractSkeleton(
                        interface_id=interface_id,
                        req_id=node_id,
                        file_path=rel_path,
                        first_line=_first_meaningful_line(code),
                        # Update rows refine an already registered contract;
                        # the registered record keeps its type at merge time
                        # (the model may also supply it).
                        type="",
                        name=stem,
                        relation="update",
                    )
                )
            continue

        type_hint = _classify_dir(rel_path)

        # Database bootstrap/schema modules: every CREATE TABLE in real code
        # is one table contract; several tables in one module yield several
        # rows so a merge of sibling schema edits can be told apart.
        table_names = _table_names_in_code(content)
        if table_names:
            for table_name in table_names:
                display_name = _to_pascal_case(f"{table_name}_table")
                anchor = _line_number_of(lines, f"CREATE TABLE {table_name}")
                skeletons.append(
                    ContractSkeleton(
                        interface_id=f"{node_id}-DB-{_normalize_id_segment(display_name)}",
                        req_id=node_id,
                        file_path=rel_path,
                        first_line=anchor if anchor != "1" else f"CREATE TABLE {table_name}",
                        type="DB",
                        name=display_name,
                        relation="owned",
                        table_name=table_name,
                    )
                )
            continue

        if type_hint == "API" and "express" in content and ("Router()" in content or "router." in content):
            mount = _EXPRESS_MOUNT_RE.search(content)
            first_method = _ROUTER_METHOD_RE.search(content)
            name = _skeleton_name(rel_path, stem, suffix, "API")
            skeletons.append(
                ContractSkeleton(
                    interface_id=f"{node_id}-API-{_normalize_id_segment(name)}",
                    req_id=node_id,
                    file_path=rel_path,
                    first_line=_first_meaningful_line(code),
                    type="API",
                    name=name,
                    relation="owned",
                    method=(first_method.group(1) if first_method else None),
                    mount_path=(mount.group(1) if mount else (first_method.group(2) if first_method else None)),
                )
            )
            continue

        type_guess = type_hint or ("UI" if suffix in {"tsx", "jsx", "vue", "svelte"} else "FUNC" if suffix in {"js", "ts", "mjs", "cjs", "py"} else None)
        if type_guess is None:
            continue

        exports = _export_names(content)
        # Naming convention from real runs: page/component files take the
        # component/export name (`LoginPage`), while service/repository/api
        # modules take the module stem (`auth_service.js` -> `AuthService`,
        # matching run5's `REQ-1-FUNC-AuthService` for the same file shape).
        if type_guess == "UI":
            display_name = exports[0] if exports else _to_pascal_case(stem)
        else:
            display_name = _skeleton_name(rel_path, stem, suffix, type_guess)
        skeletons.append(
            ContractSkeleton(
                interface_id=f"{node_id}-{type_guess}-{_normalize_id_segment(display_name)}",
                req_id=node_id,
                file_path=rel_path,
                first_line=_first_meaningful_line(code),
                type=type_guess,
                name=display_name,
                relation="owned",
                export_names=exports,
            )
        )

    return skeletons


def _normalize_id_segment(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", str(text or "").strip()).strip("-")
    return cleaned or "Contract"


def _is_reused_row(record: dict[str, Any], node_id: str = "") -> bool:
    """Whether a model-returned row represents a reused foreign contract.

    Reused parent/dependency interfaces carry their owning node's ``req_id``
    or an explicit reuse relation. Such rows must not satisfy a current-node
    skeleton row through path matching: their ``file_path`` can be a shared
    surface the current node also touched, and letting them match would mask
    a real gap. ``node_id`` is the current node; when empty only the
    explicit relation markers are checked.
    """

    relation = str(record.get("relation") or "").strip().lower()
    if relation in {"reused", "dependency", "parent"}:
        return True
    req_id = str(record.get("req_id") or "").strip()
    return bool(node_id) and req_id != "" and req_id != node_id


def merge_filled_contracts(
    skeletons: list[ContractSkeleton],
    model_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge model-filled semantics back onto the skeleton ground truth.

    The model's job is only the two semantic fields (``responsibility`` and
    ``specification``); identity fields come from the skeleton. For each
    skeleton the model record may arrive by ``interface_id`` or by
    ``file_path`` matching — the path fallback only accepts rows that claim
    the current node's ownership, so a reused parent/dependency row with a
    stale path cannot hijack a skeleton row. A record with an unknown id and
    no file path is dropped (bare semantic blobs would masquerade as filled
    rows); a reused record anchored to a real file path passes through.
    Unknown-type model records that match no skeleton keep their declared
    ``type``; every skeleton-derived record keeps the mechanical
    type/identity and never inherits an invented file path.
    """

    by_id = {str(item.get("interface_id") or "").strip(): item for item in model_records if isinstance(item, dict) and str(item.get("interface_id") or "").strip()}
    by_path: dict[str, dict[str, Any]] = {}
    for item in model_records:
        if not isinstance(item, dict) or _is_reused_row(item):
            continue
        path = _strip_workspace_prefix(str(item.get("file_path") or ""))
        if path and path not in by_path:
            by_path[path] = item

    merged: list[dict[str, Any]] = []
    consumed: set[int] = set()
    for skeleton in skeletons:
        record = by_id.get(skeleton.interface_id)
        if record is None and skeleton.file_path in by_path:
            record = by_path[skeleton.file_path]
        if record is not None and _is_reused_row(record, skeleton.req_id):
            # A reused foreign contract never fills a current-node skeleton
            # row, even when its file_path matches a shared surface.
            record = None
        if record is not None:
            consumed.add(id(record))
        semantics = record or {}
        responsibility = str(semantics.get("responsibility") or "").strip()
        specification = str(semantics.get("specification") or "").strip()
        record_type = str(semantics.get("type") or "").strip().upper()
        # Update rows carry no mechanical type (the registered contract owns
        # it); when neither the skeleton nor the model names a valid type the
        # workflow's _prepare_interfaces would reject the row, so fall back
        # to the file's directory classification.
        resolved_type = skeleton.type or record_type
        if resolved_type not in INTERFACE_TYPES:
            resolved_type = _classify_dir(skeleton.file_path) or "FUNC"
        merged.append(
            {
                "interface_id": skeleton.interface_id,
                "req_id": skeleton.req_id,
                "type": resolved_type,
                "name": skeleton.name,
                "file_path": skeleton.file_path,
                "first_line": skeleton.first_line,
                "responsibility": responsibility,
                "specification": specification,
                "callers": semantics.get("callers") or [],
                "callees": semantics.get("callees") or [],
                "inputs": semantics.get("inputs") or {},
                "outputs": semantics.get("outputs") or {},
                "test_focus": semantics.get("test_focus") or "",
                "relation": skeleton.relation,
            }
        )

    # Extra model records pass through only when they are anchored to a real
    # file path — reused parent/dependency interfaces the model added beyond
    # the mechanical list. Bare semantic blobs with unknown ids would
    # masquerade as filled rows (they match no skeleton and invent no
    # path), so they are dropped here; the caller's gap logic re-asks.
    for item in model_records:
        if isinstance(item, dict) and id(item) not in consumed:
            path = _strip_workspace_prefix(str(item.get("file_path") or ""))
            if path:
                merged.append(dict(item))

    return merged
