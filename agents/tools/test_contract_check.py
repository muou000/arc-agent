"""Static satisfiability check for generated E2E/Integration tests.

The REQ-1 failure loop (11 runtime E2E failures) came from a dual-blind
mismatch: the generated tests drove labels/selectors the requirement
specified, the implementation rendered different ones, and nobody compared
the two until Playwright timed out at IMPLEMENT time. This module moves that
comparison to generation time, mechanically:

1. extract the observable hooks a test file relies on (Playwright
   ``getBy*``/``locator`` selectors, URLs, and Integration HTTP calls);
2. build the satisfiability universe from the sources of truth the
   implementation will be built from — the requirement text and the DESIGN
   interface specifications;
3. classify each hook: ``grounded`` (found in a source), ``test_contract``
   (found nowhere, so the test *defines* it and the implementer must be
   told), or ``contradicted`` (the requirement names a different value for
   the same slot — not currently detectable mechanically, see notes);
4. feed ``test_contract`` hooks to the TestDrivenDeveloper through the node
   session so the implementation aligns to them instead of guessing.

The checker is deliberately conservative: it only extracts hook shapes it
recognizes and never fails a generation pass on its own — unmatched hooks
become declared contract hooks, not rejections. False "grounded" verdicts
are harmless (the hook reaches the implementer either way through the
interface contract); the point is closing the dual-blind gap, not policing
the test author.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ContractHook:
    """One observable value a test drives and expects the app to provide."""

    kind: str  # "label" | "text" | "role" | "placeholder" | "testid" | "url" | "api"
    value: str
    source: str  # "requirement" | "interface:<id>" | "test_contract"
    file_path: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value, "source": self.source, "file_path": self.file_path}


#: Playwright locator calls whose first argument is an accessible name the
#: app must render, keyed by the concrete method (``getByLabel('用户名')``,
#: ``getByText('Sign out')``, ``getByPlaceholder('…')``).
_BY_ACCESSIBLE_NAME = re.compile(
    r"\bgetBy(Label|Text|Placeholder|Title|AltText)\(\s*(['\"])(.+?)\2",
    re.DOTALL,
)
#: ``getByRole('button', { name: '下一步' })`` — the role itself plus the
#: name option; the name is the hook the app must provide. ``name`` may not
#: be the first option (``{ exact: true, name: ... }``), so the options
#: prefix is any run of non-name properties. ``\s`` + DOTALL also covers the
#: multi-line call form.
_BY_ROLE_NAME = re.compile(
    r"\bgetByRole\(\s*(['\"])(\w+)\1\s*,\s*\{(?:[^{}]|\{[^{}]*\})*?\bname\s*:\s*(?:(['\"])(.+?)\3|/(.+?)/)",
    re.DOTALL,
)
#: ``page.goto('/register')``, ``toHaveURL(/\/register$/)`` and the
#: ``goto(new URL('/login', base))`` form, relative links. The regex
#: alternative must tolerate escaped slashes (``\/``) inside the pattern
#: body, so each unit is an escaped char or a non-slash char.
_URL_HOOK = re.compile(
    r"(?:goto|toHaveURL)\(\s*(?:new\s+URL\(\s*)?(?:(['\"])([^'\"]+)\1|/((?:\\.|[^/\\])+?)/)",
    re.DOTALL,
)
#: test-id selectors (``getByTestId('submit-btn')``, ``locator('[data-testid="x"]')``).
_TEST_ID = re.compile(r"(?:getByTestId\(\s*|\[data-testid\s*=\s*)(['\"])([\w.-]+)\1")
#: Integration/API calls: ``fetch('/api/auth/register'...)``,
#: ``request.get('/api/auth/me')``, supertest ``agent.post('/api/auth/register')``.
_API_HOOK = re.compile(
    r"\.(?:get|post|put|patch|delete)\(\s*(['\"])(/[^'\"]+)\1",
)

_ROLE_NAMES = {"link", "button", "checkbox", "textbox", "combobox", "heading", "banner", "navigation", "img", "radio", "option", "listbox", "dialog"}

#: Cap per file on extracted hooks — pathological files must not blow up the
#: node session; the cap keeps the hook list usable as a prompt block.
_MAX_HOOKS_PER_FILE = 60


def extract_test_hooks(file_path: str, content: str) -> list[dict[str, str]]:
    """Extract the observable hooks a test file drives.

    ``file_path`` is the workspace-relative manifest path, recorded on every
    hook so the implementer knows which test needs it.
    """

    hooks: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str) -> None:
        value = value.strip()
        if not value or (kind, value) in seen:
            return
        # Degenerate forms (a bare "/" from "goto('/')", a regex fragment
        # that normalizes to symbols only) carry no implementable surface.
        if not re.search(r"[\w\u4e00-\u9fff]", value):
            return
        if len(hooks) >= _MAX_HOOKS_PER_FILE:
            return
        seen.add((kind, value))
        hooks.append({"kind": kind, "value": value, "file_path": file_path})

    for match in _BY_ACCESSIBLE_NAME.finditer(content):
        kind = {"Label": "label", "Text": "text", "Placeholder": "placeholder", "Title": "title", "AltText": "alt"}[match.group(1)]
        for piece in match.group(3).split("|"):
            add(kind, piece)
    for match in _BY_ROLE_NAME.finditer(content):
        # The accessible *name* is the hook; a role without a name is too
        # generic to pin on the implementer. Regex names may offer
        # alternatives ("a|b") — each is a satisfiable name.
        names = [match.group(4)] if match.group(4) else ([] if not match.group(5) else match.group(5).split("|"))
        for piece in names:
            add("role", piece)
    for match in _TEST_ID.finditer(content):
        add("testid", match.group(2))
    for match in _URL_HOOK.finditer(content):
        target = match.group(2) or match.group(3)
        if target:
            # Regex patterns escape their slashes (\/register) and may carry
            # anchors ($, ^); store the readable URL form so the hook block a
            # prompt shows is the real path.
            add("url", target.replace("\\/", "/").strip("$^"))
    for match in _API_HOOK.finditer(content):
        add("api", match.group(2))
    return hooks


def build_satisfiability_universe(
    requirement_data: dict[str, Any],
    interfaces: list[dict[str, Any]],
) -> str:
    """Flatten every source of truth into one searchable text blob.

    Matching later is plain substring search over this blob, so the blob must
    contain the exact surface forms (Chinese labels, routes, option strings)
    the requirement and interface specs spell out.
    """

    parts: list[str] = []
    description = str(requirement_data.get("description") or "")
    if description:
        parts.append(description)
    name = str(requirement_data.get("name") or "")
    if name:
        parts.append(name)
    for scenario in requirement_data.get("scenarios") or []:
        if not isinstance(scenario, dict):
            continue
        for key in ("given", "when", "then", "name"):
            text = str(scenario.get(key) or "").strip()
            if text:
                parts.append(text)
    for interface in interfaces or []:
        if not isinstance(interface, dict):
            continue
        interface_id = str(interface.get("interface_id") or "").strip()
        for key in ("specification", "responsibility", "first_line"):
            text = str(interface.get(key) or "").strip()
            if text:
                parts.append(f"{interface_id} {text}" if interface_id else text)
    return "\n".join(parts)


def classify_test_hooks(
    hooks: list[dict[str, str]],
    universe: str,
) -> dict[str, Any]:
    """Split extracted hooks into grounded vs test-contract declarations.

    A hook ``grounded`` in the requirement or an interface spec needs no
    extra handoff — the implementer already receives those texts. A hook
    found NOWHERE is a value the test invented; it is not a defect (the
    TestGenerator is allowed to define stable hooks), but it must be declared
    to the implementer or the run repeats the REQ-1 dual-blind loop.
    """

    blob = universe or ""
    grounded: list[dict[str, str]] = []
    test_contract: list[dict[str, str]] = []
    for hook in hooks:
        value = str(hook.get("value") or "")
        if not value:
            continue
        normalized = _normalize_for_match(value)
        if normalized and normalized in _normalize_for_match(blob):
            grounded.append({**hook, "source": "requirement-or-interface"})
        else:
            test_contract.append({**hook, "source": "test_contract"})
    return {"grounded": grounded, "test_contract": test_contract}


_URL_NOISE_PREFIXES = ("http://localhost", "http://127.0.0.1", "https://", "http://", "about:", "data:")


def _normalize_for_match(value: str) -> str:
    """Lowercase and collapse whitespace; drop pure-URL noise values.

    Full origins (``http://localhost:3301``) match nothing in the universe by
    design — only path/label surface forms are meaningful hooks. Regex URL
    fragments (``\\/register$``) are reduced to their literal core.
    """

    text = (value or "").strip().lower()
    if not text:
        return ""
    for prefix in _URL_NOISE_PREFIXES:
        if text.startswith(prefix):
            stripped = text[len(prefix):]
            # Drop the origin, keep the path (``http://host/register`` ->
            # ``/register``); a bare origin is noise and normalizes to "".
            text = stripped.split("/", 1)[-1] if "/" in stripped else ""
            break
    text = text.replace("\\/", "/").rstrip("$^")
    return " ".join(text.split())


def collect_manifest_hooks(
    workspace_root: str | Path,
    tests: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Read every E2E/Integration manifest file and extract its hooks.

    Unit tests are skipped: they exercise internal seams (imports, function
    signatures) that the interface contract already pins; selector-level
    drift is an E2E/Integration phenomenon.
    """

    hooks: list[dict[str, str]] = []
    seen_files: set[str] = set()
    for item in tests or []:
        if not isinstance(item, dict):
            continue
        test_type = str(item.get("type") or "").strip().lower()
        if test_type not in {"e2e", "integration"}:
            continue
        file_path = str(item.get("file_path") or "").strip()
        if not file_path or file_path in seen_files:
            continue
        seen_files.add(file_path)
        absolute = Path(workspace_root) / file_path
        try:
            content = absolute.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hooks.extend(extract_test_hooks(file_path, content))
    return hooks


def format_test_contract_context(test_contract: list[dict[str, str]]) -> str:
    """Render the declared hooks as the prompt block for TestDrivenDeveloper."""

    if not test_contract:
        return ""
    lines: list[str] = [
        "The generated tests drive the following observable hooks that are NOT spelled out in the",
        "requirement or the interface contract. The tests define them as the stable contract the",
        "implementation must align to: render these exact accessible names / test-ids / routes,",
        "or the corresponding test will fail.",
    ]
    by_kind: dict[str, list[dict[str, str]]] = {}
    for hook in test_contract:
        by_kind.setdefault(str(hook.get("kind") or ""), []).append(hook)
    for kind in sorted(by_kind):
        lines.append(f"- {kind}:")
        for hook in by_kind[kind]:
            lines.append(f"  - `{hook['value']}` (used by {hook.get('file_path') or 'unknown file'})")
    return "\n".join(lines)
