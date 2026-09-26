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
from typing import Any, Mapping


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
    r"\.(?:get|post|put|patch|delete)\(\s*(['\"`])(/[^'\"`]+)\1",
)

_ROLE_NAMES = {"link", "button", "checkbox", "textbox", "combobox", "heading", "banner", "navigation", "img", "radio", "option", "listbox", "dialog"}

#: Cap per file on extracted hooks — pathological files must not blow up the
#: node session; the cap keeps the hook list usable as a prompt block.
_MAX_HOOKS_PER_FILE = 60

_IDENTIFIER = r"[A-Za-z_$][\w$]*"
_HTTP_METHODS = "get|post|put|patch|delete|head|options"
_EXACT_STATUS_MATCHERS = {
    "toBe",
    "toEqual",
    "toStrictEqual",
    "toBeOneOf",
    "toContain",
    "toHaveProperty",
    "expect",
}
_STATUS_ASSERTION_RE = re.compile(
    rf"(?P<full>expect\(\s*(?P<receiver>{_IDENTIFIER})\s*\.\s*"
    rf"(?P<property>status(?:Code)?)\s*(?:\(\s*\))?\s*\)\s*\.\s*"
    rf"(?P<matcher>to[A-Za-z]+)\(\s*(?P<expected>[^;\n]*)\))",
)
_ASSERT_STATUS_RE = re.compile(
    rf"(?P<full>assert\.(?P<matcher>strictEqual|equal)\(\s*"
    rf"(?P<receiver>{_IDENTIFIER})\s*\.\s*(?P<property>status(?:Code)?)"
    rf"\s*(?:\(\s*\))?\s*,\s*(?P<expected>[^;\n]*)\))",
)
_REQUEST_CALL_RE = re.compile(
    rf"(?:(?:const|let|var)\s+(?P<variable>{_IDENTIFIER})\s*=\s*)?"
    rf"(?:await\s+)?(?:(?:{_IDENTIFIER})\s*\.\s*)+"
    rf"(?P<method>{_HTTP_METHODS})\s*\(\s*"
    rf"(?P<quote>['\"`])(?P<path>/[^'\"`\n]*)",
    re.IGNORECASE,
)
_STATUS_PROPERTY_ASSERTION_RE = re.compile(
    rf"(?P<full>expect\(\s*(?P<receiver>{_IDENTIFIER})\s*\)\s*\.\s*"
    rf"(?P<matcher>toHaveProperty)\(\s*['\"](?P<property>status(?:Code)?)['\"]"
    rf"(?:\s*,\s*(?P<expected>[^)\n]+))?\s*\))",
    re.IGNORECASE,
)
_REVERSED_STATUS_CONTAINS_RE = re.compile(
    rf"(?P<full>expect\(\s*(?P<expected>\[[^\]]*\])\s*\)\s*\.\s*"
    rf"(?P<matcher>toContain)\(\s*(?P<receiver>{_IDENTIFIER})\s*\.\s*"
    rf"status(?:Code)?\s*(?:\(\s*\))?\s*\))",
    re.IGNORECASE,
)
_REQUEST_EXPECT_RE = re.compile(
    rf"(?P<full>(?:await\s+)?(?:{_IDENTIFIER}(?:\([^\n)]*\))?\s*\.\s*)+"
    rf"(?P<method>{_HTTP_METHODS})\s*\(\s*(?P<quote>['\"`])(?P<path>/[^'\"`\n]*)"
    rf"[^\n;]*?\)\s*\.\s*expect\(\s*(?P<expected>[^)\n]+)\))",
    re.IGNORECASE,
)
_CHAIN_REQUEST_CALL_RE = re.compile(
    rf"(?:(?:const|let|var)\s+(?P<variable>{_IDENTIFIER})\s*=\s*)?"
    rf"(?:await\s+)?{_IDENTIFIER}\s*\([^)]*\)\s*\.\s*"
    rf"(?P<method>{_HTTP_METHODS})\s*\(\s*"
    rf"(?P<quote>['\"`])(?P<path>/[^'\"`\n]*)",
    re.IGNORECASE,
)
_FETCH_CALL_RE = re.compile(
    rf"(?:(?:const|let|var)\s+(?P<variable>{_IDENTIFIER})\s*=\s*)?"
    rf"(?:await\s+)?fetch\(\s*(?P<quote>['\"])(?P<path>/[^'\"\n]*)",
    re.IGNORECASE,
)
_ROUTE_DECLARATION_RE = re.compile(
    rf"\b(?:router|app|server|api)\s*\.\s*(?P<method>{_HTTP_METHODS})\s*\(\s*"
    rf"(?P<quote>['\"`])(?P<path>/[^'\"`\n]*)",
    re.IGNORECASE,
)
_ROUTE_DECORATOR_RE = re.compile(
    rf"@(?:app|router)\s*\.\s*(?P<method>{_HTTP_METHODS})\s*\(\s*"
    rf"(?P<quote>['\"])(?P<path>/[^'\"\n]*)"
    rf"(?P<options>[^)]*)\)",
    re.IGNORECASE,
)
_ROUTE_STATUS_RE = re.compile(
    r"(?:\.(?:status|sendStatus|code)\s*\(\s*|\b(?:statusCode|status_code)\s*[:=]\s*|\bctx\.status\s*=\s*)"
    r"(?P<code>[1-5]\d{2})\b",
    re.IGNORECASE,
)
_ROUTE_RETURN_STATUS_RE = re.compile(r"\breturn\b[\s\S]{0,160}?,\s*(?P<code>[1-5]\d{2})\b")
_ROUTE_STATUS_COMMENT_RE = re.compile(r"^[ \t]*//[ \t]*(?P<code>[1-5]\d{2})[ \t]*(?:->|=>|:|：|\{)[ \t]*\S", re.MULTILINE)
# Route-table comment with the status after the path: `// POST /api/auth/register -> 201`.
_ROUTE_STATUS_ARROW_COMMENT_RE = re.compile(
    rf"^[ \t]*//[ \t]*(?:{_HTTP_METHODS})?[ \t]*(?P<path>/[^\s]*)[ \t]*(?:->|=>|→)[ \t]*(?P<code>[1-5]\d{{2}})\b",
    re.IGNORECASE | re.MULTILINE,
)
_ROUTE_PLACEHOLDER_RE = re.compile(r"\bNOT_IMPLEMENTED\b|\bTODO\s*\(\s*TDD\s*\)", re.IGNORECASE)
_ROUTE_STATEMENT_RE = re.compile(r"(?:[^;'\"]|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")*;", re.DOTALL)
_STATUS_CODE_LIST_RE = re.compile(
    r"\b(?:HTTP\s+)?status\s+codes?\s+(?:fixed|allowed|supported)\s*:\s*(?P<values>[^.\n]*)",
    re.IGNORECASE,
)
_STATUS_TEXT_PATTERNS = (
    re.compile(
        r"\b(?:HTTP\s+)?status(?:\s+code|_code)?\s*(?:is|=|:|->|returns?|returned|为|是)\s*(?P<code>[1-5]\d{2})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:returns?|returning|responds?\s+with|response\s+is|expects?)\s+"
        r"(?:HTTP\s+)?(?:status(?:\s+code)?\s*(?:is|=|:)?\s*)?"
        r"(?P<code>[1-5]\d{2})\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bHTTP\s+(?P<code>[1-5]\d{2})\b", re.IGNORECASE),
    re.compile(
        r"(?:返回|响应)\s*(?:HTTP\s*)?(?:状态码\s*)?(?:为|是|[:：])?\s*"
        r"(?P<code>[1-5]\d{2})\b"
    ),
    re.compile(r"状态码\s*(?:为|是|[:：])?\s*(?P<code>[1-5]\d{2})\b"),
    # Route-table arrow notation: `POST /api/auth/register -> 201`. The
    # leading path token anchors the arrow so prose arrows stay ignored.
    re.compile(
        r"(?P<path>/[A-Za-z0-9_./:{}?=&%-]+)[ \t]*(?:->|=>|→)[ \t]*(?P<code>[1-5]\d{2})\b",
        re.IGNORECASE,
    ),
    # Pipe-separated status alternatives: `-> 200 detail | 404 NOT_FOUND` or
    # `-> 200 { ok } | 404 { error } | 400 { error }`. DESIGN writes `|` as
    # the clause boundary between a route's alternative response statuses.
    re.compile(r"\|[ \t]*(?P<code>[1-5]\d{2})\b", re.IGNORECASE),
    # Status-body continuation: `; 400 { errors: ... }` (also line start /
    # text start). A clause boundary plus a bare status-sized number with an
    # opening body brace is the shape DESIGN writes after the primary arrow.
    re.compile(
        r"(?:^|[;,\n]|\b(?:or|and)\b|(?:或|或者))[ \t]*"
        r"(?P<code>[1-5]\d{2})[ \t]*\{",
        re.IGNORECASE,
    ),
)
_STATUS_FIELD_NAMES = {
    "status",
    "statuscode",
    "status_code",
    "httpstatus",
    "http_status",
    "responsestatus",
    "response_status",
    "statuses",
    "statuscodes",
    "status_codes",
}


def _normalize_api_path(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if text.startswith("/workspace/"):
        text = text[len("/workspace") :]
    if not text.startswith("/"):
        return ""
    text = text.split("?", 1)[0].split("#", 1)[0]
    text = re.sub(r"/+$", "", text)
    return text or "/"


def _parse_status_codes(value: Any) -> list[int]:
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value] if 100 <= value <= 599 else []
    if isinstance(value, float) and value.is_integer():
        code = int(value)
        return [code] if 100 <= code <= 599 else []
    text = str(value or "")
    codes: list[int] = []
    for raw in re.findall(r"(?<!\d)([1-5]\d{2})(?!\d)", text):
        code = int(raw)
        if code not in codes:
            codes.append(code)
    return codes


def _extract_status_codes_from_text(value: Any) -> list[int]:
    text = str(value or "")
    codes: list[int] = []
    for match in _STATUS_CODE_LIST_RE.finditer(text):
        for code in _parse_status_codes(match.group("values")):
            if code not in codes:
                codes.append(code)
    for pattern in _STATUS_TEXT_PATTERNS:
        for match in pattern.finditer(text):
            code = int(match.group("code"))
            if code not in codes:
                codes.append(code)
    return codes


_METHOD_PATH_RE = re.compile(
    rf"\b(?P<method>{_HTTP_METHODS})\s+(?P<path>/[A-Za-z0-9_./:{{}}?=&%-]+)",
    re.IGNORECASE,
)


def _route_status_declarations(value: Any) -> list[dict[str, Any]]:
    """Extract only statuses in each method/path clause of an interface."""

    text = str(value or "")
    matches = list(_METHOD_PATH_RE.finditer(text))
    declarations = []
    for index, match in enumerate(matches):
        clause = text[match.start(): matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        # A free-form summary after a route list does not belong only to
        # the final listed route. Structured outputs can still supply it.
        clause = _STATUS_CODE_LIST_RE.split(clause, maxsplit=1)[0]
        codes = _extract_status_codes_from_text(clause)
        for arrow in re.finditer(r"(?:->|=>|→)\s*([1-5]\d{2})\b", clause):
            code = int(arrow.group(1))
            if code not in codes:
                codes.append(code)
        for comment_match in _ROUTE_STATUS_COMMENT_RE.finditer(clause):
            code = int(comment_match.group("code"))
            if code not in codes:
                codes.append(code)
        if codes:
            declarations.append({
                "method": match.group("method").upper(),
                "path": _normalize_api_path(match.group("path").rstrip(".,;")),
                "status_codes": codes,
            })
    return declarations


def preserve_prior_api_route_clauses(previous: str, updated: str) -> str:
    """Retain statuses of old routes absent from a reused card's update."""

    prior = _route_status_declarations(previous)
    current = _route_status_declarations(updated)
    matches = list(_METHOD_PATH_RE.finditer(previous))
    retained = []
    for index, match in enumerate(matches):
        method = match.group("method").upper()
        path = _normalize_api_path(match.group("path").rstrip(".,;"))
        if not any(route["method"] == method and route["path"] == path for route in prior):
            continue
        if any(
            route["method"] == method and _path_matches(route["path"], path)
            for route in current
        ):
            continue
        clause = previous[match.start(): matches[index + 1].start() if index + 1 < len(matches) else len(previous)]
        retained.append(clause.strip())
    return " ".join([updated.strip(), *retained]).strip()


def _extract_status_codes_from_value(value: Any, *, key_hint: str = "") -> list[int]:
    if isinstance(value, dict):
        codes: list[int] = []
        for raw_key, nested in value.items():
            key = re.sub(r"[^a-z0-9_]", "", str(raw_key).lower())
            if key in _STATUS_FIELD_NAMES or (
                key == "code" and key_hint in {"outputs", "responses", "response"}
            ):
                candidates = _parse_status_codes(nested)
                if not candidates and isinstance(nested, str):
                    candidates = _extract_status_codes_from_text(nested)
            else:
                candidates = _extract_status_codes_from_value(
                    nested, key_hint=key_hint if key_hint in {"outputs", "responses", "response"} else key
                )
            for code in candidates:
                if code not in codes:
                    codes.append(code)
        return codes
    if isinstance(value, (list, tuple, set)):
        codes: list[int] = []
        for item in value:
            for code in _extract_status_codes_from_value(item, key_hint=key_hint):
                if code not in codes:
                    codes.append(code)
        return codes
    if key_hint in _STATUS_FIELD_NAMES or key_hint in {"outputs", "responses", "response"}:
        return _parse_status_codes(value)
    return _extract_status_codes_from_text(value)


def _workspace_relative_path(raw_path: str) -> str:
    relative = str(raw_path or "").strip().replace("\\", "/")
    if relative.startswith("/workspace/"):
        relative = relative[len("/workspace/") :]
    return relative.lstrip("/")


def _read_workspace_text(workspace_root: str | Path, raw_path: str) -> str:
    root = Path(workspace_root).expanduser().resolve()
    relative = _workspace_relative_path(raw_path)
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return ""
    try:
        return candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _extract_api_requests(content: str) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for pattern in (_REQUEST_CALL_RE, _CHAIN_REQUEST_CALL_RE, _FETCH_CALL_RE):
        for match in pattern.finditer(content):
            path = _normalize_api_path(match.group("path"))
            if not path:
                continue
            method = str(match.groupdict().get("method") or "").upper()
            if not method and pattern is _FETCH_CALL_RE:
                call_end = content.find(")", match.end())
                options = content[match.end() : call_end if call_end != -1 else match.end() + 400]
                method_match = re.search(r"\bmethod\s*:\s*['\"]([A-Za-z]+)", options, re.IGNORECASE)
                method = str(method_match.group(1) if method_match else "GET").upper()
            requests.append(
                {
                    "path": path,
                    "method": method or "GET",
                    "variable": str(match.groupdict().get("variable") or "").strip(),
                    "start": match.start(),
                }
            )
    return sorted(requests, key=lambda item: int(item["start"]))


def _status_values_from_assertion(matcher: str, raw_expected: str) -> list[int]:
    if matcher not in _EXACT_STATUS_MATCHERS:
        return []
    if matcher == "expect" and not re.fullmatch(
        r"\s*(?:[1-5]\d{2}|\[[^\]]*\])(?:\s*,[^)]*)?\s*", raw_expected
    ):
        return []
    return _parse_status_codes(raw_expected)


def extract_http_status_assertions(file_path: str, content: str) -> list[dict[str, Any]]:
    """Extract exact HTTP status assertions and their nearest request.

    This deliberately recognizes only assertions that name a concrete status
    code. Range assertions such as ``>= 200`` remain visible with an empty
    ``expected_status_codes`` list so DESIGN can emit a needs-info diagnostic
    instead of treating an arbitrary 2xx as contract-safe.
    """

    requests = _extract_api_requests(content)
    matches = (
        list(_STATUS_ASSERTION_RE.finditer(content))
        + list(_ASSERT_STATUS_RE.finditer(content))
        + list(_STATUS_PROPERTY_ASSERTION_RE.finditer(content))
        + list(_REVERSED_STATUS_CONTAINS_RE.finditer(content))
    )
    assertions: list[dict[str, Any]] = []
    for match in _REQUEST_EXPECT_RE.finditer(content):
        groups = match.groupdict()
        matcher = "expect"
        expected = str(groups.get("expected") or "").strip()
        expected_codes = _status_values_from_assertion(matcher, expected)
        if not expected_codes:
            continue
        assertions.append(
            {
                "file_path": file_path,
                "line": content.count("\n", 0, match.start()) + 1,
                "path": _normalize_api_path(groups.get("path", "")),
                "method": str(groups.get("method") or "").upper(),
                "assertion": str(groups.get("full") or "").strip(),
                "expected_status_codes": expected_codes,
                "matcher": matcher,
            }
        )
    for match in sorted(matches, key=lambda item: item.start()):
        groups = match.groupdict()
        receiver = str(groups.get("receiver") or "").strip()
        request = next(
            (
                item
                for item in reversed(requests)
                if int(item["start"]) <= match.start()
                and (not receiver or item["variable"] == receiver)
            ),
            None,
        )
        raw_expected = str(groups.get("expected") or "").strip()
        matcher = str(groups.get("matcher") or "").strip()
        assertions.append(
            {
                "file_path": file_path,
                "line": content.count("\n", 0, match.start()) + 1,
                "path": request["path"] if request else None,
                "method": request["method"] if request else None,
                "assertion": str(groups.get("full") or "").strip(),
                "expected_status_codes": _status_values_from_assertion(matcher, raw_expected),
                "matcher": matcher,
            }
        )
    return assertions


def collect_manifest_http_status_assertions(
    workspace_root: str | Path,
    tests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Read E2E/Integration manifest files and extract HTTP status assertions."""

    assertions: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    for item in tests or []:
        if not isinstance(item, dict):
            continue
        test_type = str(item.get("type") or "").strip().lower()
        if test_type not in {"e2e", "integration"}:
            continue
        file_path = str(item.get("file_path") or "").strip().replace("\\", "/")
        if not file_path or file_path in seen_files:
            continue
        seen_files.add(file_path)
        content = _read_workspace_text(workspace_root, file_path)
        if content:
            assertions.extend(extract_http_status_assertions(file_path, content))
    return assertions


def _leading_comment_block(content: str, position: int) -> str:
    """Return the contiguous ``//`` comment block ending directly above ``position``.

    Design skeletons state each route's contract in a comment table placed
    above the route declarations; per-declaration segments start at the
    declaration itself, so without this the comment table belongs to no
    segment. A blank or code line stops the block.
    """

    lines = content[:position].splitlines(keepends=True)
    index = len(lines)
    while index > 0 and lines[index - 1].lstrip().startswith("//"):
        index -= 1
    return "".join(lines[index:])


def _route_declaration_keys(content: str) -> set[tuple[str, str]]:
    """Return method/path pairs for executable route declarations."""

    routes: set[tuple[str, str]] = set()
    for match in _ROUTE_DECLARATION_RE.finditer(content):
        line = content[content.rfind("\n", 0, match.start()) + 1 : match.start()].strip()
        if line.startswith(("//", "/*", "*")):
            continue
        routes.add(
            (
                match.group("method").upper(),
                _normalize_api_path(match.group("path")),
            )
        )
    return routes


def _extract_route_records(workspace_root: str | Path, interface: dict[str, Any]) -> list[dict[str, Any]]:
    file_path = str(interface.get("file_path") or "").strip()
    content = _read_workspace_text(workspace_root, file_path)
    if not content:
        return []
    matches = list(_ROUTE_DECLARATION_RE.finditer(content))
    matches.extend(_ROUTE_DECORATOR_RE.finditer(content))
    matches.sort(key=lambda item: item.start())
    routes: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        leading = _leading_comment_block(content, match.start())
        segment = leading + content[match.start() : end]
        codes: list[int] = []
        comment_routes = _route_status_declarations(leading)
        same_method = [
            route for route in comment_routes
            if route["method"] == match.group("method").upper()
        ]
        declared = [
            route for route in same_method
            if (
                _path_matches(match.group("path"), route["path"])
                or (match.group("path") == "/" and len(same_method) == 1)
            )
        ]
        for route in declared:
            for code in route["status_codes"]:
                if code not in codes:
                    codes.append(code)
        for comment_match in _ROUTE_STATUS_COMMENT_RE.finditer(content[match.start() : end]):
            code = int(comment_match.group("code"))
            if code not in codes:
                codes.append(code)
        # Scaffold responses describe unfinished code, not the intended contract.
        executable = re.sub(r"/\*[\s\S]*?\*/|^[ \t]*//[^\n]*", "", segment, flags=re.MULTILINE)
        placeholder_statements = [
            (statement.start(), statement.end())
            for statement in _ROUTE_STATEMENT_RE.finditer(executable)
            if _ROUTE_PLACEHOLDER_RE.search(statement.group())
        ]

        def is_placeholder(position: int) -> bool:
            return any(start <= position < stop for start, stop in placeholder_statements)

        for status_match in _ROUTE_STATUS_RE.finditer(executable):
            if is_placeholder(status_match.start()):
                continue
            code = int(status_match.group("code"))
            if code not in codes:
                codes.append(code)
        for status_match in _ROUTE_RETURN_STATUS_RE.finditer(executable):
            if is_placeholder(status_match.start()):
                continue
            code = int(status_match.group("code"))
            if code not in codes:
                codes.append(code)
        for option_match in re.finditer(r"\bstatus_code\s*=\s*([1-5]\d{2})\b", executable, re.IGNORECASE):
            if is_placeholder(option_match.start()):
                continue
            code = int(option_match.group(1))
            if code not in codes:
                codes.append(code)
        if codes:
            routes.append(
                {
                    "method": str(match.group("method") or "").upper(),
                    "path": _normalize_api_path(match.group("path")),
                    "status_codes": codes,
                    "source": f"route:{file_path}",
                }
            )
    return routes


def _extract_interface_route_paths(interface: dict[str, Any]) -> list[dict[str, str]]:
    paths: list[dict[str, str]] = []
    fields = (
        ("name", interface.get("name")),
        ("responsibility", interface.get("responsibility")),
        ("specification", interface.get("specification")),
        ("first_line", interface.get("first_line")),
    )
    for _field, value in fields:
        text = str(value or "")
        method_matches = list(
            re.finditer(
                r"\b(?P<method>(?:" + _HTTP_METHODS + r"))\s+"
                r"(?P<path>/[A-Za-z0-9_./:{}?=&%-]+)",
                text,
                re.IGNORECASE,
            )
        )
        for match in method_matches:
            path = _normalize_api_path(match.group("path").rstrip(".,;"))
            if path:
                paths.append({"method": match.group("method").upper(), "path": path})
        for raw_path in re.findall(r"['\"](/[^'\"\s)]+)", text):
            path = _normalize_api_path(raw_path.rstrip(".,;"))
            if path and not any(item["path"] == path for item in paths):
                paths.append({"method": "", "path": path})
    return paths


def find_unregistered_api_routes(
    workspace_root: str | Path,
    materialized_paths: list[str],
    interfaces: list[dict[str, Any]],
    *,
    baseline_contents: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """Find newly materialized API routes lacking a method/path contract card.

    ``materialized_paths`` includes edits to shared template surfaces. The
    optional baseline snapshot lets callers exclude route declarations that
    already existed before this DESIGN pass, while keeping new declarations
    on the same shared file fail-closed.
    """

    contracts = [
        route
        for interface in interfaces
        if str(interface.get("type") or "").upper() == "API"
        for route in _extract_interface_route_paths(interface)
        if route["method"]
    ]
    baselines = baseline_contents or {}
    missing: list[dict[str, str]] = []
    for file_path in dict.fromkeys(materialized_paths):
        content = _read_workspace_text(workspace_root, file_path)
        if not content:
            continue
        baseline_routes = _route_declaration_keys(
            baselines.get(_workspace_relative_path(file_path), "")
        )
        for match in _ROUTE_DECLARATION_RE.finditer(content):
            line = content[content.rfind("\n", 0, match.start()) + 1:match.start()].strip()
            if line.startswith(("//", "/*", "*")):
                continue
            method = match.group("method").upper()
            relative_path = _normalize_api_path(match.group("path"))
            if (method, relative_path) in baseline_routes:
                continue
            # A router-relative '/' cannot identify its mount without the
            # app's wiring; a specific path or a route-table comment can.
            declared = [
                route for route in _route_status_declarations(_leading_comment_block(content, match.start()))
                if route["method"] == method
                and _path_matches(relative_path, route["path"])
            ]
            path = declared[-1]["path"] if declared else relative_path
            if path == "/":
                continue
            if any(
                route["method"] == method and _path_matches(relative_path, route["path"])
                for route in contracts
            ) or (
                declared and any(
                    route["method"] == method and _path_matches(route["path"], path)
                    for route in contracts
                )
            ):
                continue
            entry = {"file_path": str(file_path), "method": method, "path": path}
            if entry not in missing:
                missing.append(entry)
    return missing


def _interface_status_sources(workspace_root: str | Path, interface: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for field in ("specification", "responsibility", "first_line", "test_focus"):
        codes = _extract_status_codes_from_text(interface.get(field))
        if codes:
            sources.append({"codes": codes, "source": f"interface:{interface.get('interface_id')}:{field}"})
    for field in (
        "outputs",
        "response",
        "responses",
        "status",
        "status_code",
        "status_codes",
        "http_status",
        "response_status",
        "expected_status",
        "expected_status_codes",
    ):
        codes = _extract_status_codes_from_value(interface.get(field), key_hint=field)
        if codes:
            sources.append({"codes": codes, "source": f"interface:{interface.get('interface_id')}:{field}"})
    for route in _extract_route_records(workspace_root, interface):
        sources.append({"codes": route["status_codes"], "source": route["source"], "route": route})
    return sources


def _path_matches(candidate: str, requested: str) -> bool:
    left = _normalize_api_path(candidate)
    right = _normalize_api_path(requested)
    if not left or not right:
        return False
    if left == right:
        return True
    if left == "/":
        return right == "/"
    if right.endswith(left):
        return True
    return _parameterized_path_matches(left, right)


def _parameterized_path_matches(candidate: str, requested: str) -> bool:
    """Express ``:param``/``*`` segments match one non-empty concrete segment.

    Interface cards and route skeletons declare parameterized routes
    (``GET /api/workbooks/:id/state``) while generated tests drive concrete
    paths (``/api/workbooks/q3-sales/state``); a literal-only comparison can
    never hit those contracts. Segments keep the suffix semantics of the
    literal match: a shorter candidate aligns to the tail of the requested
    path, which covers router-relative declarations under a mount prefix.
    A single-segment relative parameter route (``/:id``) needs at least two
    swallowed segments: with one it would reinterpret the resource segment
    of a sibling static route (``/:id`` vs ``/api/workbooks``) as the
    parameter value. Routers in this system mount at multi-segment prefixes
    (``app.use('/api/workbooks', router)``), so the mount a relative route
    swallows is never a single segment.
    """

    pattern = candidate.strip("/").split("/")
    if not any(segment.startswith(":") or segment.startswith("*") for segment in pattern):
        return False
    concrete = requested.strip("/").split("/")
    if len(pattern) > len(concrete):
        return False
    swallow = len(concrete) - len(pattern)
    if swallow and swallow < (2 if len(pattern) == 1 else 1):
        return False
    aligned = concrete[swallow:]
    for expected, actual in zip(pattern, aligned):
        if expected.startswith(":") or expected.startswith("*"):
            if not actual:
                return False
        elif expected != actual:
            return False
    return True


def _static_path_matches(candidate: str, requested: str) -> bool:
    """Match without parameter reinterpretation: literal equality or suffix.

    Route precedence rule: when an assertion path statically matches a
    declared route, sibling parameterized records that reach it only by
    reinterpreting a static segment as a parameter value describe different
    endpoints (``/:id`` vs ``/api/auth/register``) and must not contribute
    their statuses to that assertion's verdict.
    """

    left = _normalize_api_path(candidate)
    right = _normalize_api_path(requested)
    if not left or not right:
        return False
    if left == right:
        return True
    if left == "/":
        return right == "/"
    return right.endswith(left)


def _assertion_status_codes(
    item: dict[str, Any], path: str, method: str
) -> tuple[list[int], list[int]]:
    """The contract one candidate would validate an assertion against.

    Returns ``(route_codes, registered_codes)``: route codes come from the
    candidate's routes matching this assertion's path/method (all route codes
    when the card has no route records); registered codes prefer the
    interface card's declared codes. The single-candidate verdict and the
    ambiguity disambiguation share this so both compute the contract the
    same way.
    """

    matching_routes = [
        route
        for route in item["routes"]
        if (
            (not path or _path_matches(route.get("path", ""), path))
            and (not method or not route.get("method") or route.get("method") == method)
        )
    ]
    if path and any(
        _static_path_matches(route.get("path", ""), path)
        and (not method or not route.get("method") or route.get("method") == method)
        for route in item["paths"]
    ):
        # The assertion path statically matches a declared path of this
        # candidate: route records that reach it only through parameter
        # reinterpretation (``/:id`` vs the list route ``/api/workbooks`` or
        # the sibling ``/api/auth/register``) describe different endpoints,
        # and their codes must not enter this assertion's verdict.
        matching_routes = [
            route
            for route in matching_routes
            if _static_path_matches(route.get("path", ""), path)
        ]
    matched_route_codes: list[int] = []
    for route in matching_routes:
        for code in route.get("status_codes") or []:
            if code not in matched_route_codes:
                matched_route_codes.append(code)
    route_codes = matched_route_codes if item["routes"] else item["route_codes"]
    declared = [
        route for route in item["declared_routes"]
        if (not path or _path_matches(route["path"], path))
        and (not method or route["method"] == method)
    ]
    declared_codes = list(dict.fromkeys(
        code for route in declared for code in route["status_codes"]
    ))
    registered_codes = (
        declared_codes if item["declared_routes"]
        else list(item["interface_codes"] or route_codes)
    )
    return route_codes, registered_codes


def _status_contract_diagnostic(
    *,
    code: str,
    assertion: dict[str, Any],
    message: str,
    contract_status_codes: list[int] | None = None,
    interface_id: str = "",
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "file_path": assertion.get("file_path", ""),
        "line": assertion.get("line"),
        "assertion": assertion.get("assertion", ""),
        "path": assertion.get("path"),
        "method": assertion.get("method"),
        "expected_status_codes": list(assertion.get("expected_status_codes") or []),
        "contract_status_codes": list(contract_status_codes or []),
        "interface_id": interface_id,
    }


def _manifest_interface_ids(tests: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Interface ids each manifest test row declares, keyed by file path.

    Conflict reporting prefers a candidate the manifest actually names; a
    missing or empty ``interface_ids`` field simply contributes nothing.
    """

    declared: dict[str, set[str]] = {}
    for item in tests or []:
        if not isinstance(item, dict):
            continue
        file_key = str(item.get("file_path") or "").strip().replace("\\", "/")
        raw_ids = item.get("interface_ids")
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        ids = {str(value).strip() for value in raw_ids or [] if str(value).strip()}
        if file_key and ids:
            declared.setdefault(file_key, set()).update(ids)
    return declared


def validate_http_status_contracts(
    workspace_root: str | Path,
    requirement_data: dict[str, Any],
    interfaces: list[dict[str, Any]],
    tests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate generated HTTP status assertions against DESIGN contracts.

    Status codes are resolved in this order: an exact status in the
    requirement, an explicit status in the interface card, then an explicit
    status in the route source file. There is intentionally no implicit 200
    fallback: an assertion without one of those sources is a needs-info
    diagnostic.
    """

    assertions = collect_manifest_http_status_assertions(workspace_root, tests)
    if not assertions:
        return []
    api_interfaces = [
        interface
        for interface in interfaces or []
        if isinstance(interface, dict) and str(interface.get("type") or "").strip().upper() == "API"
    ]
    requirement_text = "\n".join(
        str(requirement_data.get(key) or "")
        for key in ("name", "description")
    )
    for scenario in requirement_data.get("scenarios") or []:
        if isinstance(scenario, dict):
            requirement_text += "\n" + "\n".join(str(scenario.get(key) or "") for key in ("name", "given", "when", "then"))
    requirement_codes = _extract_status_codes_from_text(requirement_text)
    requirement_routes = _route_status_declarations(requirement_text)
    requirement_paths = [
        _normalize_api_path(path.rstrip(".,;"))
        for path in re.findall(r"['\"](/[^'\"\s)]+)", requirement_text)
    ]
    manifest_interface_ids = _manifest_interface_ids(tests)
    interface_types = {
        str(interface.get("interface_id") or "").strip():
            str(interface.get("type") or "").strip().upper()
        for interface in interfaces or []
        if isinstance(interface, dict)
    }

    prepared: list[dict[str, Any]] = []
    for interface in api_interfaces:
        paths = _extract_interface_route_paths(interface)
        sources = _interface_status_sources(workspace_root, interface)
        interface_codes: list[int] = []
        route_codes: list[int] = []
        for source in sources:
            route = source.get("route")
            target = interface_codes if route is None else route_codes
            for code in source.get("codes") or []:
                if code not in target:
                    target.append(code)
            if route:
                route_path = {"method": route.get("method", ""), "path": route.get("path", "")}
                if route_path not in paths:
                    paths.append(route_path)
        prepared.append(
            {
                "interface": interface,
                "interface_id": str(interface.get("interface_id") or "").strip(),
                "paths": paths,
                "interface_codes": interface_codes,
                "declared_routes": [
                    route
                    for field in ("specification", "responsibility", "first_line", "test_focus")
                    for route in _route_status_declarations(interface.get(field))
                    if route["status_codes"]
                ],
                "route_codes": route_codes,
                "routes": [
                    source["route"]
                    for source in sources
                    if isinstance(source.get("route"), dict)
                ],
            }
        )

    diagnostics: list[dict[str, Any]] = []
    for assertion in assertions:
        path = str(assertion.get("path") or "").strip()
        method = str(assertion.get("method") or "").strip().upper()
        candidates = [
            item
            for item in prepared
            if (
                not path
                or not item["paths"]
                or any(
                    _path_matches(route.get("path", ""), path)
                    and (not method or not route.get("method") or route.get("method") == method)
                    for route in item["paths"]
                )
            )
        ]
        if path:
            path_candidates = [item for item in candidates if item["paths"]]
            if path_candidates:
                candidates = path_candidates
        if not candidates and len(prepared) == 1 and not prepared[0]["paths"]:
            candidates = prepared
        if not candidates:
            if path == "/":
                # The bare root path is the SPA/static surface the backend
                # serves, not an API route. When this test file's manifest
                # binds it to a non-API interface (FUNC/UI), the root
                # assertion belongs to that contract, which the API status
                # gate does not police; unknown or API-only owners keep the
                # needs-info diagnostic.
                named_ids = manifest_interface_ids.get(
                    str(assertion.get("file_path") or ""), set()
                )
                if any(
                    interface_types.get(named_id, "") not in ("", "API")
                    for named_id in named_ids
                ):
                    continue
            diagnostics.append(
                _status_contract_diagnostic(
                    code="status_code_needs_info",
                    assertion=assertion,
                    message=(
                        "needs-info: the status assertion has no matching API interface or route "
                        f"contract for {path or 'the request'}; do not guess an HTTP status code."
                    ),
                )
            )
            continue
        if len(candidates) > 1:
            matching = [
                item
                for item in candidates
                if path
                and any(_path_matches(route.get("path", ""), path) for route in item["paths"])
            ]
            if matching:
                candidates = matching
                # A bare no-method path (the quoted ``app.use('/x', ...)``
                # extraction) is ownership-neutral: when another candidate
                # matches the assertion's method, bare-path-only candidates
                # step out of the contest instead of manufacturing ambiguity.
                if method:
                    method_matched = [
                        item
                        for item in candidates
                        if any(
                            _path_matches(route.get("path", ""), path)
                            and route.get("method") == method
                            for route in item["paths"]
                        )
                    ]
                    if method_matched:
                        candidates = method_matched
                if len(candidates) > 1:
                    # Candidates whose registered status sets are identical
                    # are interchangeable: any choice yields the same
                    # verdict, so proceed instead of reporting ambiguity.
                    code_sets = [
                        frozenset(_assertion_status_codes(item, path, method)[1])
                        for item in candidates
                    ]
                    shared = code_sets[0]
                    if shared and all(codes == shared for codes in code_sets[1:]):
                        named_ids = manifest_interface_ids.get(
                            str(assertion.get("file_path") or ""), set()
                        )
                        named = [
                            item for item in candidates if item["interface_id"] in named_ids
                        ]
                        candidates = [named[0] if named else candidates[0]]
        candidate = candidates[0] if len(candidates) == 1 else None
        if candidate is None:
            diagnostics.append(
                _status_contract_diagnostic(
                    code="status_code_needs_info",
                    assertion=assertion,
                    message=(
                        "needs-info: more than one API interface could own this status assertion; "
                        "name the route or interface status before generating an exact assertion."
                    ),
                )
            )
            continue

        candidate_paths = [route.get("path", "") for route in candidate["paths"]]
        route_codes, registered_codes = _assertion_status_codes(candidate, path, method)
        named_ids = manifest_interface_ids.get(str(assertion.get("file_path") or ""), set())
        if named_ids and candidate["interface_id"] not in named_ids:
            diagnostics.append(
                _status_contract_diagnostic(
                    code="status_code_needs_info",
                    assertion=assertion,
                    interface_id=candidate["interface_id"],
                    message=(
                        f"needs-info: this test's manifest must name API interface "
                        f"{candidate['interface_id']} for {method} {path}, not only "
                        f"{', '.join(sorted(named_ids))}. Use coverage_scope=dependency or shared "
                        "for a foreign regression contract."
                    ),
                )
            )
            continue
        matched_requirement = [
            route for route in requirement_routes
            if (not path or _path_matches(route["path"], path))
            and (not method or route["method"] == method)
        ]
        relevant_requirement_codes = (
            list(dict.fromkeys(code for route in matched_requirement for code in route["status_codes"]))
            if requirement_routes else requirement_codes
        )
        requirement_applies = bool(
            relevant_requirement_codes
            and (
                not requirement_paths
                or not path
                or any(_path_matches(req_path, path) for req_path in requirement_paths)
                or len(prepared) == 1
            )
        )
        if registered_codes and route_codes and set(registered_codes).isdisjoint(route_codes):
            diagnostics.append(
                _status_contract_diagnostic(
                    code="status_code_conflict",
                    assertion=assertion,
                    interface_id=candidate["interface_id"],
                    contract_status_codes=registered_codes,
                    message=(
                        f"HTTP status sources for {candidate['interface_id'] or 'API interface'} "
                        f"conflict: interface declares {', '.join(str(code) for code in registered_codes)}, "
                        f"but the matched route declares {', '.join(str(code) for code in route_codes)}."
                    ),
                )
            )
            continue
        if requirement_applies and registered_codes and set(relevant_requirement_codes).isdisjoint(registered_codes):
            diagnostics.append(
                _status_contract_diagnostic(
                    code="status_code_conflict",
                    assertion=assertion,
                    interface_id=candidate["interface_id"],
                    contract_status_codes=registered_codes,
                    message=(
                        f"Requirement declares HTTP status {', '.join(str(code) for code in relevant_requirement_codes)}, "
                        f"but registered contract {candidate['interface_id'] or 'API interface'} declares "
                        f"{', '.join(str(code) for code in registered_codes)}."
                    ),
                )
            )
            continue
        contract_codes = (
            list(relevant_requirement_codes)
            if requirement_applies
            else registered_codes
        )
        if not contract_codes:
            diagnostics.append(
                _status_contract_diagnostic(
                    code="status_code_needs_info",
                    assertion=assertion,
                    interface_id=candidate["interface_id"],
                    message=(
                        "needs-info: not enough HTTP status contract information exists for "
                        f"{path or ', '.join(candidate_paths) or 'this request'}; the contract "
                        "does not declare an HTTP status code. Remove the guessed assertion or "
                        "record the exact status in the interface contract."
                    ),
                )
            )
            continue
        expected = list(assertion.get("expected_status_codes") or [])
        if not expected:
            diagnostics.append(
                _status_contract_diagnostic(
                    code="status_code_needs_info",
                    assertion=assertion,
                    interface_id=candidate["interface_id"],
                    contract_status_codes=contract_codes,
                    message=(
                        "needs-info: HTTP status assertions must name a specific status code "
                        f"from the contract ({', '.join(str(code) for code in contract_codes)}); "
                        "a range or arbitrary 2xx matcher is not contract-safe."
                    ),
                )
            )
            continue
        if not set(expected).issubset(set(contract_codes)):
            diagnostics.append(
                _status_contract_diagnostic(
                    code="status_code_conflict",
                    assertion=assertion,
                    interface_id=candidate["interface_id"],
                    contract_status_codes=contract_codes,
                    message=(
                        f"HTTP status assertion in {assertion['file_path']} conflicts with the "
                        f"registered contract {candidate['interface_id'] or 'API interface'}: "
                        f"assertion expects {', '.join(str(code) for code in expected)}, "
                        f"contract permits {', '.join(str(code) for code in contract_codes)}. "
                        "Update the test to match the contract before TDD."
                    ),
                )
            )
    return diagnostics


def format_http_status_diagnostics(diagnostics: list[dict[str, Any]]) -> str:
    """Render deterministic DESIGN diagnostics with source locations."""

    lines = ["DESIGN failed: HTTP status contract validation rejected generated tests."]
    for diagnostic in diagnostics:
        location = str(diagnostic.get("file_path") or "unknown test file")
        line = diagnostic.get("line")
        if line:
            location += f":{line}"
        assertion = str(diagnostic.get("assertion") or "").strip()
        suffix = f" Assertion: `{assertion}`." if assertion else ""
        lines.append(f"- {diagnostic.get('code', 'status_code_needs_info')} at {location}.{suffix}")
        lines.append(f"  {diagnostic.get('message', '')}")
    return "\n".join(lines)


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
