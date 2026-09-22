"""Write-time static import validation for test assets (issue #156).

The 2026-09-22 easy-ticketbooking serial run lost an entire IMPLEMENT pass
($4.52 / 12.7min / 143 calls) to two *mechanically detectable* import defects
in a DESIGN-produced test file: a relative import written one ``../`` short
(``../src/app`` from ``tests/integration/`` resolved into ``/tests/src/app``)
and an ESM import without its ``.js`` extension. The TDD loop burned the full
Integration budget flip-flopping between ``../src`` and ``../../src`` with no
signal about which direction was right.

This module is the mechanical half of the answer: parse the ESM import
specifiers out of a test file's would-be content, resolve each relative one
against the workspace the file is being written into, and classify the
failures so the block message can name the exact correction ("did you mean
``../../src/app.js``?"). It is deliberately a heuristic scanner, not a JS
parser — every shape it cannot understand is fail-open (skipped, counted as
a warning), because a static check that dead-locks legitimate writes would
only be a new rework generator (the run7 lesson).

Deliberately out of scope:

- bare specifiers (``vitest``, ``@testing-library/react``) and bundler
  aliases (``@/components/x``) — resolved by the package system, not the
  workspace tree;
- CommonJS ``require()`` — Node's CJS resolver appends extensions and index
  files itself, so an extensionless require is not a defect;
- non-literal dynamic imports (``await import(specifierVar)``) — returned
  separately as *opaque* so the caller can fail open with a warning count;
- non-JS test files (the CLI app type's ``test_*.py``) — ESM rules do not
  apply to them.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Callable

from agents.runtime.capabilities import normalize_manifest_path

#: Extensions tried when a specifier does not name a file exactly. Superset
#: of Vite's default ``resolve.extensions`` so a frontend test importing a
#: ``.tsx`` component classifies as "missing extension", not "missing file".
#: ``.json`` last: a resolved JSON import is legal but never the intended
#: correction for a missing JS module, so it must not shadow a JS hit.
_SOURCE_EXTENSIONS: tuple[str, ...] = (
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts", ".json",
)

#: Target-file extensions the validation applies to. Anything else (the CLI
#: app type's ``test_*.py``, Android sources) is out of ESM territory.
JS_SOURCE_TARGET_EXTENSIONS: frozenset[str] = frozenset(
    {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
)

#: Depth corrections tried when a specifier resolves to nothing: one ``../``
#: more, one fewer, then two more / two fewer. The observed flip-flop was
#: ``../src`` vs ``../../src`` (+1); the symmetric directions keep a too-deep
#: specifier from dead-ending in "missing" when one ``../`` less is exact.
_DEPTH_DELTAS: tuple[int, ...] = (1, -1, 2, -2)

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
# The whitespace lookbehind keeps ``https://`` inside a string literal from
# being read as a line comment; ``^`` (with MULTILINE) catches a comment that
# opens a line.
_COMMENT_LINE = re.compile(r"(?:^|(?<=\s))//.*$", re.MULTILINE)
_IMPORT_FROM = re.compile(
    r"""\b(?:import|export)\b(?:(?!\b(?:import|export)\b)[\s\S])*?\bfrom\s*['"]([^'"\n]+)['"]"""
)
_IMPORT_SIDE_EFFECT = re.compile(r"""\bimport\s*['"]([^'"\n]+)['"]""")
_IMPORT_DYNAMIC = re.compile(r"""\bimport\s*\(\s*['"]([^'"\n]+)['"]""")
# import( with anything but a string literal: runtime path construction the
# scanner must not guess at.
_IMPORT_DYNAMIC_OPAQUE = re.compile(r"""\bimport\s*\(\s*(?!['"])""")


def is_js_source_path(path: str) -> bool:
    """Whether a (virtual or workspace-relative) path is a JS/TS source file."""

    normalized = normalize_manifest_path(path).lower()
    return any(normalized.endswith(ext) for ext in JS_SOURCE_TARGET_EXTENSIONS)


def strip_js_comments(text: str) -> str:
    """Drop JS block and line comments so documented imports never trip the gate."""

    text = _COMMENT_BLOCK.sub(" ", text)
    return _COMMENT_LINE.sub("", text)


def extract_relative_esm_imports(content: str) -> tuple[list[str], int]:
    """Relative ESM import specifiers in a file's content, plus opaque count.

    Returns ``(specifiers, opaque_dynamic_imports)``: the relative
    (``./``/``../``) string-literal specifiers in first-appearance order, and
    the number of dynamic ``import(...)`` calls whose argument is not a
    string literal — those are the caller's fail-open warning signal. Bare
    specifiers and aliases never appear in the list.
    """

    code = strip_js_comments(content)
    matches: list[tuple[int, str]] = []
    opaque = len(_IMPORT_DYNAMIC_OPAQUE.findall(code))
    for pattern in (_IMPORT_FROM, _IMPORT_SIDE_EFFECT, _IMPORT_DYNAMIC):
        for match in pattern.finditer(code):
            specifier = _strip_query_fragment(match.group(1))
            if specifier.startswith(("./", "../")):
                matches.append((match.start(), specifier))
    specifiers: list[str] = []
    seen: set[str] = set()
    for _position, specifier in sorted(matches, key=lambda item: item[0]):
        if specifier not in seen:
            seen.add(specifier)
            specifiers.append(specifier)
    return specifiers, opaque


@dataclass(frozen=True)
class ImportViolation:
    """One import a static check can prove broken, with its exact fix."""

    specifier: str
    #: ``extension`` = right depth, target exists only with an explicit
    #: extension; ``depth`` = a ±1/±2 ``../`` correction lands on a file;
    #: ``missing`` = nothing at or near the specifier exists.
    kind: str
    #: Workspace-relative path the specifier (or its best correction)
    #: resolves to, when one was found.
    resolved: str
    #: The corrected specifier the model should have written, when known.
    suggestion: str


def classify_import(
    specifier: str,
    importer_dir: str,
    exists: Callable[[str], bool],
) -> ImportViolation | None:
    """Classify one relative specifier, or ``None`` when it resolves.

    ``importer_dir`` is the workspace-relative directory of the file being
    written (``backend/tests/integration``); ``exists`` answers for a
    workspace-relative path whether the target file is real (template
    skeleton on disk or materialized by this session). The search order is
    exact specifier first, then extension/index completion, then depth
    corrections — so a specifier that names a real file as written never
    produces a style suggestion, and every suggestion names a file that
    actually exists.
    """

    base = _join_from(importer_dir, specifier)
    if not base:
        # A literal relative import that climbs above the workspace is
        # statically known to be invalid. This is not the fail-open case:
        # dynamic expressions and aliases are opaque, while this path is
        # fully resolved and cannot name a workspace file.
        return ImportViolation(
            specifier=specifier,
            kind="missing",
            resolved="<outside workspace>",
            suggestion="",
        )

    exact = base if exists(base) else None
    if exact is not None:
        return None

    for resolved_rel, suffix in _completion_candidates(base):
        if exists(resolved_rel):
            return ImportViolation(
                specifier=specifier,
                kind="extension",
                resolved=resolved_rel,
                suggestion=specifier + suffix,
            )

    for delta in _DEPTH_DELTAS:
        corrected = _adjust_depth(specifier, delta)
        if corrected is None:
            continue
        corrected_base = _join_from(importer_dir, corrected)
        if corrected_base and exists(corrected_base):
            return ImportViolation(
                specifier=specifier,
                kind="depth",
                resolved=corrected_base,
                suggestion=corrected,
            )
        for resolved_rel, suffix in _completion_candidates(corrected_base):
            if exists(resolved_rel):
                return ImportViolation(
                    specifier=specifier,
                    kind="depth",
                    resolved=resolved_rel,
                    suggestion=corrected + suffix,
                )

    return ImportViolation(specifier=specifier, kind="missing", resolved=base, suggestion="")


def build_import_block_message(target_path: str, violations: list[ImportViolation]) -> str:
    """One block message listing every broken import with its correction.

    All violations land in a single message on purpose: the serial-run file
    carried two defects, and one-round-trip fixes are the difference between
    a nudge and a rework generator.
    """

    target_rel = normalize_manifest_path(target_path) or target_path
    lines = [
        f"Test import blocked: {len(violations)} relative ESM import(s) in {target_rel} "
        "do not resolve against the workspace files. Fix them in this write:"
    ]
    for violation in violations:
        if violation.kind == "extension":
            lines.append(
                f"- '{violation.specifier}': the target exists only as "
                f"'{violation.resolved}' — relative ESM imports need the explicit "
                f"extension, use '{violation.suggestion}'."
            )
        elif violation.kind == "depth":
            lines.append(
                f"- '{violation.specifier}': resolves to '{violation.resolved}' which does not "
                f"exist — did you mean '{violation.suggestion}'?"
            )
        else:
            lines.append(
                f"- '{violation.specifier}': no file at '{violation.resolved}' and no "
                "depth/extension correction lands on an existing file. Import only files "
                "that exist (template skeletons or files written in this session); create "
                "the target module first if this test needs it."
            )
    return "\n".join(lines)


def _join_from(importer_dir: str, specifier: str) -> str:
    """Workspace-relative path a specifier resolves to from ``importer_dir``.

    Returns ``""`` when the resolution escapes the workspace root — such a
    target cannot exist and must not be probed on the real filesystem.
    """

    base = posixpath.normpath(posixpath.join(importer_dir, specifier)) if importer_dir else posixpath.normpath(specifier)
    if base.startswith("..") or base == ".":
        return ""
    return base


def _completion_candidates(base: str) -> list[tuple[str, str]]:
    """``(workspace_relative_path, specifier_suffix)`` pairs for extension and index resolution.

    The suffix is what must be appended to the *specifier* to name the
    resolved file directly (``.js`` for extension completion, ``/index.js``
    for directory-index completion).
    """

    candidates: list[tuple[str, str]] = []
    for ext in _SOURCE_EXTENSIONS:
        candidates.append((base + ext, ext))
    index_base = posixpath.join(base, "index")
    for ext in _SOURCE_EXTENSIONS:
        candidates.append((posixpath.join(index_base) + ext, f"/index{ext}"))
    return candidates


def _adjust_depth(specifier: str, delta: int) -> str | None:
    """Specifier with ``delta`` extra (or fewer) leading ``../`` segments.

    ``None`` when the correction is a no-op or would strip more ``../``
    segments than the specifier has. A correction that strips the last
    ``../`` keeps the result relative with an explicit ``./`` — a bare
    ``src/app`` would silently turn into a package lookup.
    """

    if delta == 0:
        return None
    if delta > 0:
        return "../" * delta + specifier
    climb = 0
    for segment in specifier.split("/"):
        if segment != "..":
            break
        climb += 1
    if climb + delta < 0:
        return None
    remaining = climb + delta
    body = specifier[len("../") * climb :]
    corrected = "../" * remaining + body
    if not corrected.startswith("."):
        corrected = "./" + corrected
    return corrected if corrected != specifier else None


def _strip_query_fragment(specifier: str) -> str:
    """Drop Vite ``?query`` / ``#fragment`` suffixes before resolution."""

    for separator in ("?", "#"):
        position = specifier.find(separator)
        if position != -1:
            specifier = specifier[:position]
    return specifier
