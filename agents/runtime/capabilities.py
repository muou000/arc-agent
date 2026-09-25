"""Declarative capability table for ARC's staged agent workflow.

"May stage X call tool Y on path P?" has exactly one authoritative answer:
the table in this module. Three consumers read it so the judgment can never
drift apart again:

- ``StageDisciplineMiddleware`` blocks categorically-denied calls with the
  table's message (dynamic state guards — write budgets, manifest locks,
  session ownership — stay in the middleware; they are runtime conditions,
  not static capabilities).
- ``build_stage_agent`` mounts only tools the table allows for the stage and
  derives the always-disabled builtin set from it.
- Prompt restatements of tool availability are pinned to the table by
  ``tests/test_agents/test_stage_capabilities.py`` (the messages stay
  hand-written; a drift in either direction fails the consistency tests).

The module is deliberately dependency-free inside ``agents``: it sits below
``stage_discipline``, ``test_manifest`` and ``factory`` in the import graph
and hosts the shared path-predicate vocabulary those modules used to
duplicate.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable, Literal

Stage = Literal["interface_design", "test_generation", "implementation"]

#: Stages the capability table enumerates. Kept as a tuple so loops over the
#: table (consistency tests, derived sets) stay deterministic.
STAGES: tuple[Stage, ...] = ("interface_design", "test_generation", "implementation")

# The stage-pipeline test domain is deliberately independent from a concrete
# app type.  App handlers keep their existing test roots (for example
# ``backend/tests`` or ``tests``); the stable ``generated/<node>`` segment is
# the ownership boundary shared by all of them.
NODE_TEST_NAMESPACE_ROOT = "tests/generated"

_SHARED_TEST_RESOURCE_BASENAMES = frozenset(
    {
        "playwright.config.js",
        "playwright.config.cjs",
        "playwright.config.ts",
        "vitest.config.js",
        "vitest.config.ts",
        "jest.config.js",
        "jest.config.ts",
        "setup-tests.js",
        "setup-tests.ts",
        "setuptests.js",
        "setuptests.ts",
        "conftest.py",
    }
)
_SHARED_TEST_RESOURCE_PATHS = frozenset(
    {
        "frontend/test/setup.ts",
        "frontend/test/setup.js",
        "backend/src/database/test_harness.js",
        "backend/src/database/prepare_e2e.js",
    }
)
_SHARED_TEST_RESOURCE_SEGMENTS = (
    "/tests/fixtures/",
    "/test/fixtures/",
    "/tests/helpers/",
    "/test/helpers/",
    "/tests/__fixtures__/",
    "/test/__fixtures__/",
    "/tests/__mocks__/",
    "/test/__mocks__/",
)

# ---------------------------------------------------------------------------
# Path predicates (the single implementation of each judgment)
# ---------------------------------------------------------------------------


def normalize_manifest_path(value: object) -> str:
    """Canonicalize a tool-call or manifest path to workspace-relative form.

    Accepts virtual (``/workspace/a/b.test.ts``), relative (``a/b.test.ts``,
    ``./a/b.test.ts``) and absolute workspace-root-prefixed forms. Absolute
    paths outside the workspace keep their ``/``-joined form (only used for
    diagnostics; they never match a declared relative path).
    """

    path = re.sub(r"^\\\\\?\\", "", str(value or "").strip())
    if not path:
        return ""
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if path == "/workspace" or path == "/workspace/":
        return ""
    if path.startswith("/workspace/"):
        return path[len("/workspace/") :].strip("/")
    return path.strip("/")


def stable_node_path_segment(node_id: object) -> str:
    """Return a deterministic, filesystem-safe node-id path segment.

    The segment must also be a valid Python/Java package identifier for the
    CLI and Android runners. The digest makes case and punctuation variants
    distinct on case-insensitive filesystems as well as on POSIX.
    """

    raw = str(node_id or "").strip()
    safe = re.sub(r"[^a-z0-9_]+", "_", raw.lower()).strip("_") or "node"
    if safe[0].isdigit():
        safe = f"n_{safe}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{safe}_{digest}"


def node_test_namespace(node_id: object) -> str:
    """Return the canonical stable test namespace for one requirement node."""

    return f"{NODE_TEST_NAMESPACE_ROOT}/{stable_node_path_segment(node_id)}"


def node_test_namespace_prefixes(node_id: object) -> tuple[str, ...]:
    """Return app-type test roots that carry the same node namespace.

    The canonical root is included first for diagnostics and generic/CLI
    consumers.  Web and Android handlers use their existing roots while
    retaining the same stable ``generated/<node>`` ownership segment.
    """

    segment = stable_node_path_segment(node_id)
    return (
        f"tests/generated/{segment}",
        f"frontend/tests/generated/{segment}",
        f"backend/tests/generated/{segment}",
        f"backend/test-e2e/generated/{segment}",
    )


def is_node_test_path(path: str, node_id: object) -> bool:
    """Whether a test asset belongs to the current node's stable namespace."""

    normalized = normalize_manifest_path(path).lower()
    if not normalized:
        return False
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        return False
    for prefix in node_test_namespace_prefixes(node_id):
        prefix = prefix.lower()
        if normalized == prefix or normalized.startswith(f"{prefix}/"):
            return True
    # Android's package directory is configured at runtime, so its package
    # prefix cannot be enumerated here.  The stable generated segment still
    # gives it an unambiguous node ownership boundary.
    segment = f"/generated/{stable_node_path_segment(node_id).lower()}/"
    return normalized.startswith("app/src/test/java/") and segment in f"/{normalized}/"


def is_shared_test_resource(path: str) -> bool:
    """Whether a path is runner-owned/shared test infrastructure.

    Node-local helpers and fixtures are allowed only inside their node
    namespace.  This predicate covers the well-known template resources so
    the static capability table and the dynamic middleware share one answer
    for the read-only carve-out.
    """

    normalized = normalize_manifest_path(path).lower()
    if not normalized:
        return False
    if normalized in {item.lower() for item in _SHARED_TEST_RESOURCE_PATHS}:
        return True
    if "/generated/" in f"/{normalized}/":
        # Node-specific setup and fixture files live below this boundary;
        # the node-domain guard decides which node may mutate them.
        return False
    name = normalized.rsplit("/", 1)[-1]
    if name in _SHARED_TEST_RESOURCE_BASENAMES:
        return True
    wrapped = f"/{normalized}/"
    return any(segment in wrapped for segment in _SHARED_TEST_RESOURCE_SEGMENTS)


def is_test_asset(path: str) -> bool:
    """Whether a path is a test *asset*: anything the TestGenerator may write.

    Broader than :func:`is_test_file_path` — helpers and runner configs
    (``setup-tests.ts``, ``playwright.config.js``) count, because the stage
    must write them without a manifest entry.
    """

    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    # ``/test-e2e/`` is the web app type's E2E directory: its files may carry
    # any JS/TS source name (the placement rule accepts plain names), so the
    # segment must count as a test asset even without a `.test.`/`.spec.`
    # marker — otherwise a declared `backend/test-e2e/login.js` would be
    # rejected as "not a test asset" after passing the manifest declaration.
    test_segments = ("/test/", "/tests/", "/__tests__/", "/e2e/", "/test-e2e/", "/__mocks__/")
    test_names = (".test.", ".spec.", "playwright.config.", "vitest.config.", "jest.config.", "setup-tests.", "setuptests.")
    return any(segment in normalized for segment in test_segments) or any(marker in name for marker in test_names)


def is_not_test_asset(path: str) -> bool:
    """Negation of :func:`is_test_asset`, usable as a table predicate."""

    return not is_test_asset(path)


def is_test_file_path(path: str) -> bool:
    """Whether a path is a *test file* (not a helper/config).

    The manifest predicate: it governs which paths the TestGenerator must
    declare through ``declare_test_manifest`` before writing. Deliberately
    narrower than :func:`is_test_asset` — helpers and runner configs are test
    *assets* the stage may write freely, and locking them would only add
    blocked-turn noise.

    Files whose name marks them as tests — JavaScript-style ``.test.``/
    ``.spec.`` names plus the Python unittest conventions
    ``test_*.py``/``*_test.py`` (the CLI app type's layout) — plus the one
    name-agnostic case: web E2E files under a ``test-e2e`` directory, where
    the app-type rule accepts any JS/TS source name.
    """

    normalized = normalize_manifest_path(path).lower()
    if "/test-e2e/" in f"/{normalized}/":
        return True
    name = normalized.rsplit("/", 1)[-1]
    if ".test." in name or ".spec." in name:
        return True
    if not name.endswith(".py"):
        return False
    return name.startswith("test_") or name.endswith("_test.py")


# ---------------------------------------------------------------------------
# Verdicts and rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """The table's answer for one (stage, tool, path) cell.

    ``message`` is the middleware's block text when ``allowed`` is False; a
    ``{path}`` placeholder is replaced with the call's path. Empty when
    allowed.
    """

    allowed: bool
    message: str = field(default="")


#: Shared answer for every uncategorized call. Default-open matches the
#: historical middleware semantics: traceability queries, ``ls``/``glob``/
#: ``grep`` and future tools pass through, and their containment is the
#: filesystem permission layer's job, not this table's.
ALLOW = Verdict(allowed=True)


@dataclass(frozen=True)
class CapabilityRule:
    """One table row: a path predicate and the verdict it yields.

    ``path_matches`` is a pure function of the call path (``None`` means the
    rule matches regardless of path — a tool-level verdict). Rules are
    evaluated in declaration order; the first match wins.
    """

    path_matches: Callable[[str], bool] | None
    verdict: Verdict


def _disabled(tool: str) -> Verdict:
    return Verdict(allowed=False, message=f"`{tool}` is disabled in ARC's staged file workflow.")


_APPEND_ONLY_IN_DESIGN = Verdict(
    allowed=False,
    message="append_file is only available during the interface_design stage.",
)
_NO_VALIDATION_IN_TESTGEN = Verdict(
    allowed=False,
    message="TestGenerator only creates tests and its manifest; it must not run validation.",
)
_NO_VALIDATION_IN_DESIGN = Verdict(
    allowed=False,
    message="InterfaceDesigner only designs skeletons and contracts; validation belongs to TestDrivenDeveloper.",
)
_NOT_A_TEST_ASSET = Verdict(
    allowed=False,
    message=(
        "TestGenerator may write only test files, test helpers/configuration, "
        "and the returned manifest; {path} is not a test asset."
    ),
)
_SHARED_TEST_RESOURCE = Verdict(
    allowed=False,
    message=(
        "Shared test resource blocked: {path} is runner-owned test configuration or fixture "
        "and is read-only during staged execution. Keep node-specific helpers and fixtures "
        "inside the current node's generated test namespace."
    ),
)


def _build_rules() -> dict[tuple[Stage, str], tuple[CapabilityRule, ...]]:
    rules: dict[tuple[Stage, str], tuple[CapabilityRule, ...]] = {}

    # Disabled builtins: denied in every stage, never mounted.
    for stage in STAGES:
        for tool in ("execute", "write_todos"):
            rules[(stage, tool)] = (CapabilityRule(path_matches=None, verdict=_disabled(tool)),)

    # delete: every stage denies it except the two declared channels.
    # The channels themselves carry runtime conditions the table cannot see
    # (manifest lock, rewrite budget, session ownership) — the middleware
    # applies those after the table's static verdict allows the call.
    rules[("interface_design", "delete")] = (
        CapabilityRule(path_matches=None, verdict=_disabled("delete")),
    )
    for stage in ("test_generation", "implementation"):
        rules[(stage, "delete")] = (
            # test_generation: a green-baseline rejection may remove a test
            # asset (duplicate or tautological coverage). implementation: the
            # middleware narrows this to test files the session wrote itself.
            CapabilityRule(path_matches=is_test_asset, verdict=ALLOW),
            CapabilityRule(path_matches=None, verdict=_disabled("delete")),
        )

    # Validation tools: only the implementation stage runs builds/tests, and
    # only that stage has the tools mounted. The explicit rows for the other
    # two stages keep "no rule = allow" from answering for tools the stage
    # never sees — the table must reflect the real mount surface, not the
    # absence of a rule (issue #182).
    _validation_denials: dict[Stage, Verdict] = {
        "interface_design": _NO_VALIDATION_IN_DESIGN,
        "test_generation": _NO_VALIDATION_IN_TESTGEN,
    }
    for stage, verdict in _validation_denials.items():
        for tool in ("run_build", "run_tests"):
            rules[(stage, tool)] = (CapabilityRule(path_matches=None, verdict=verdict),)

    # append_file: the DESIGN-only additive continuation tool.
    for stage in ("test_generation", "implementation"):
        rules[(stage, "append_file")] = (
            CapabilityRule(path_matches=None, verdict=_APPEND_ONLY_IN_DESIGN),
        )

    # TestGenerator writes test assets only — via either whole-file write or
    # anchor edit. Shared-surface blocking (template wiring) is applied by the
    # middleware *before* this rule so its remediation message keeps
    # precedence for product paths.
    for tool in ("write_file", "edit_file"):
        rules[("test_generation", tool)] = (
            CapabilityRule(path_matches=is_not_test_asset, verdict=_NOT_A_TEST_ASSET),
        )

    return rules


_RULES: dict[tuple[Stage, str], tuple[CapabilityRule, ...]] = _build_rules()


def capability_for(
    stage: str,
    tool: str,
    path: str = "",
    *,
    enforce_node_test_domain: bool = False,
) -> Verdict:
    """The verdict for calling ``tool`` on ``path`` during ``stage``.

    First matching rule for ``(stage, tool)`` wins; no matching rule (or no
    rules at all) means allowed. ``{path}`` placeholders in a denial message
    are replaced with the call's path so callers can use the text verbatim.
    """

    if enforce_node_test_domain and tool in {"write_file", "edit_file", "append_file", "delete"}:
        if is_shared_test_resource(path):
            return _SHARED_TEST_RESOURCE
    for rule in _RULES.get((stage, tool), ()):
        if rule.path_matches is None or rule.path_matches(path):
            verdict = rule.verdict
            if not verdict.allowed and "{path}" in verdict.message:
                return Verdict(allowed=False, message=verdict.message.replace("{path}", path))
            return verdict
    return ALLOW


#: Builtin tools the table denies in every stage regardless of path. Derived
#: from the table's keys, not enumerated: a tool qualifies only when every
#: stage has rules for it and every rule is an unconditional denial — any
#: path-scoped rule anywhere means the tool has allowed cells (``delete`` on
#: test assets, for one) and stays mountable. Consumed by the factory's
#: harness exclusion + ``DisableToolsMiddleware``;
#: ``tests/test_agents/test_stage_capabilities.py`` pins the derivation so the
#: set and the table cannot drift apart.


def _unconditionally_denied(tool: str) -> bool:
    for stage in STAGES:
        rules = _RULES.get((stage, tool))
        if not rules:
            return False
        for rule in rules:
            if rule.path_matches is not None or rule.verdict.allowed:
                return False
    return True


DISABLED_BUILTIN_TOOLS: frozenset[str] = frozenset(
    tool for (_, tool) in _RULES if _unconditionally_denied(tool)
)
