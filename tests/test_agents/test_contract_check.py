"""Unit tests for the static test-contract satisfiability check.

``agents/tools/test_contract_check.py`` extracts the observable hooks a
generated E2E/Integration test drives (Playwright selectors, URLs, API
calls), classifies them against the requirement + interface-spec texts, and
declares the un-grounded remainder as test-contract hooks for the
implementation. Fixtures mirror the shapes observed on the test1 benchmark
run (Chinese accessible names, regex role names, supertest API paths).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.tools.test_contract_check import (
    _path_matches,
    _route_status_declarations,
    build_satisfiability_universe,
    classify_test_hooks,
    collect_manifest_hooks,
    extract_http_status_assertions,
    extract_test_hooks,
    find_unregistered_api_routes,
    format_test_contract_context,
    validate_http_status_contracts,
)


E2E_SPEC = """\
const { test, expect } = require('@playwright/test');

test('register flow', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('link', { name: 'Register' })).toBeVisible();
  await page.getByRole('link', { name: 'Register' }).click();
  await expect(page).toHaveURL(/\\/register$/);

  await page.getByLabel('用户名').fill('tb-user-abc');
  await page.getByLabel('登录密码').fill('Valid-password-123!');
  await page.getByPlaceholder('再次输入密码').fill('Valid-password-123!');
  await page.getByTestId('submit-btn').click();
  await expect(page.getByRole('button', { name: /下一步|提交/ })).toBeVisible();
  await expect(page.getByText('Sign out')).toBeVisible();

  const me = await page.request.get('/api/auth/me');
  expect(me.status()).toBe(200);
});
"""

INTEGRATION_SPEC = """\
const request = require('supertest');
const app = require('../src/app');

it('logs out', async () => {
  const agent = request.agent(app);
  await agent.post('/api/auth/register').send({});
  await agent.post('/api/auth/logout');
});
"""

UNIT_SPEC = """\
const { add } = require('../src/calc');
it('adds', () => expect(add(1, 1)).toBe(2));
"""

REQ_DATA = {
    "name": "注册旅客账号",
    "description": (
        "首页必须提供 `Register` 链接；注册页提供 `用户名`、`登录密码` 输入框，"
        "提交按钮 accessible name 为 `下一步`；有效提交后导航回 `/`。"
    ),
    "scenarios": [
        {"name": "happy path", "given": "用户在注册页", "when": "填写有效资料", "then": "创建会话并回首页"},
    ],
}

INTERFACES = [
    {
        "interface_id": "REQ-1-API-Auth",
        "type": "API",
        "specification": "register returns 200 + Set-Cookie; /me restores current user from cookie.",
        "responsibility": "Auth boundary",
        "file_path": "backend/src/routes/auth.js",
        "first_line": "router.post('/register'",
    },
]


def test_extract_hooks_from_playwright_spec() -> None:
    hooks = extract_test_hooks("backend/test-e2e/register.e2e.spec.js", E2E_SPEC)
    kinds = {(h["kind"], h["value"]) for h in hooks}
    assert ("label", "用户名") in kinds
    assert ("label", "登录密码") in kinds
    assert ("placeholder", "再次输入密码") in kinds
    assert ("role", "Register") in kinds
    assert ("testid", "submit-btn") in kinds
    assert ("url", "/register") in kinds
    assert ("api", "/api/auth/me") in kinds
    # Regex role names are split into their alternatives; both count as hooks.
    assert ("role", "下一步") in kinds
    assert ("role", "提交") in kinds
    # Every hook records its source file.
    assert all(h["file_path"] == "backend/test-e2e/register.e2e.spec.js" for h in hooks)


def test_extract_hooks_from_integration_spec() -> None:
    hooks = extract_test_hooks("backend/tests/authApi.test.js", INTEGRATION_SPEC)
    api_paths = sorted(h["value"] for h in hooks if h["kind"] == "api")
    assert api_paths == ["/api/auth/logout", "/api/auth/register"]


def test_extract_hooks_skips_degenerate_values() -> None:
    """A bare "/" or symbols-only regex fragment is not an implementable hook."""

    hooks = extract_test_hooks(
        "x.spec.js",
        "await page.goto('/');\nawait expect(page).toHaveURL(/\\/$/);\n",
    )
    assert hooks == []


def test_universe_contains_requirement_and_interface_text() -> None:
    universe = build_satisfiability_universe(REQ_DATA, INTERFACES)
    assert "用户名" in universe
    assert "Register" in universe
    assert "下一步" in universe
    assert "/me restores current user" in universe
    assert "happy path" in universe  # scenario names participate too


def test_classify_splits_grounded_and_test_contract() -> None:
    hooks = extract_test_hooks("backend/test-e2e/register.e2e.spec.js", E2E_SPEC)
    universe = build_satisfiability_universe(REQ_DATA, INTERFACES)
    result = classify_test_hooks(hooks, universe)

    grounded = {(h["kind"], h["value"]) for h in result["grounded"]}
    contract = {(h["kind"], h["value"]) for h in result["test_contract"]}
    # Spelled out in the requirement text.
    assert ("label", "用户名") in grounded
    assert ("role", "Register") in grounded
    assert ("url", "/register") in grounded
    assert ("role", "下一步") in grounded
    # Not spelled out anywhere: test-defined contract hooks.
    assert ("label", "Sign out") not in grounded
    assert ("placeholder", "再次输入密码") in contract
    assert ("testid", "submit-btn") in contract
    assert ("label", "登录密码") in grounded  # in requirement description
    # Grounded hooks carry the matched-source marker.
    assert all(h["source"] == "requirement-or-interface" for h in result["grounded"])
    assert all(h["source"] == "test_contract" for h in result["test_contract"])


def test_classify_is_case_and_whitespace_insensitive() -> None:
    hooks = [{"kind": "label", "value": "  用户名  ", "file_path": "a"}]
    result = classify_test_hooks(hooks, "registration form has 用户名 field")
    assert result["grounded"] and not result["test_contract"]


def test_collect_manifest_hooks_reads_only_e2e_and_integration(tmp_path: Path) -> None:
    e2e = tmp_path / "backend" / "test-e2e" / "spec.js"
    e2e.parent.mkdir(parents=True)
    e2e.write_text(E2E_SPEC, encoding="utf-8")
    unit = tmp_path / "tests" / "unit" / "calc.test.js"
    unit.parent.mkdir(parents=True)
    unit.write_text(UNIT_SPEC, encoding="utf-8")
    missing = tmp_path / "tests" / "integration" / "gone.test.js"

    tests = [
        {"type": "E2E", "file_path": "backend/test-e2e/spec.js"},
        {"type": "Unit", "file_path": "tests/unit/calc.test.js"},
        {"type": "Integration", "file_path": "tests/integration/gone.test.js"},
        # duplicate file path collapses
        {"type": "E2E", "file_path": "backend/test-e2e/spec.js"},
    ]
    hooks = collect_manifest_hooks(str(tmp_path), tests)
    files = {h["file_path"] for h in hooks}
    assert files == {"backend/test-e2e/spec.js"}
    assert ("label", "用户名") in {(h["kind"], h["value"]) for h in hooks}
    # Missing files are skipped silently (unwritable/moved manifests must not
    # break the DESIGN phase).


def test_format_test_contract_context_renders_hook_block() -> None:
    text = format_test_contract_context(
        [
            {"kind": "label", "value": "再次输入密码", "file_path": "e2e/spec.js"},
            {"kind": "testid", "value": "submit-btn", "file_path": "e2e/spec.js"},
        ]
    )
    assert "再次输入密码" in text
    assert "submit-btn" in text
    assert "e2e/spec.js" in text
    assert "implementation must align to" in text


def test_format_test_contract_context_empty_returns_empty() -> None:
    assert format_test_contract_context([]) == ""


def test_extract_hooks_name_not_first_role_option() -> None:
    """``{ exact: true, name: ... }`` — name after other options still matches."""

    hooks = extract_test_hooks(
        "x.spec.js",
        "page.getByRole('link', { exact: true, name: 'Register' }).click();",
    )
    assert ("role", "Register") in {(h["kind"], h["value"]) for h in hooks}


def test_extract_hooks_nested_option_object() -> None:
    """A nested option object before ``name`` must not derail the match."""

    hooks = extract_test_hooks(
        "x.spec.js",
        "page.getByRole('row', { name: 'row1' }).filter({ hasText: 'detail' });",
    )
    assert ("role", "row1") in {(h["kind"], h["value"]) for h in hooks}


def test_extract_hooks_goto_new_url_form() -> None:
    """``goto(new URL('/login', base))`` — the path is still a URL hook."""

    hooks = extract_test_hooks("x.spec.js", "await page.goto(new URL('/login', base));")
    assert ("url", "/login") in {(h["kind"], h["value"]) for h in hooks}


def test_normalize_strips_origin_to_path() -> None:
    """Full origins reduce to their path; a bare origin reduces to nothing.

    ``http://localhost:3301/register`` -> ``register`` is the surface a
    requirement could name; ``http://localhost:3301`` alone carries none.
    """

    from agents.tools.test_contract_check import _normalize_for_match

    assert _normalize_for_match("http://localhost:3301/register") == "register"
    assert _normalize_for_match("http://localhost:3301") == ""
    assert _normalize_for_match("https://example.com/api/auth") == "api/auth"
    assert _normalize_for_match(r"\/register$") == "/register"


def test_extract_http_status_assertions_keeps_request_context() -> None:
    content = """
const response = await page.request.post('/api/notes');
expect(response.status()).toBe(201);
"""

    assertions = extract_http_status_assertions("e2e/notes.spec.js", content)

    assert assertions == [
        {
            "file_path": "e2e/notes.spec.js",
            "line": 3,
            "path": "/api/notes",
            "method": "POST",
            "assertion": "expect(response.status()).toBe(201)",
            "expected_status_codes": [201],
            "matcher": "toBe",
        }
    ]


def test_extract_http_status_assertions_supports_supertest_request_chain() -> None:
    content = """
const response = await request(app).post('/api/notes');
expect(response.status).toBe(201);
"""

    assertions = extract_http_status_assertions("tests/notes.test.js", content)

    assert assertions[0]["path"] == "/api/notes"
    assert assertions[0]["method"] == "POST"
    assert assertions[0]["expected_status_codes"] == [201]


@pytest.mark.parametrize(
    ("content", "expected_codes"),
    [
        (
            "const response = await request(app).post('/api/notes').expect(201);",
            [201],
        ),
        (
            "expect(response).toHaveProperty('status', 201);",
            [201],
        ),
        (
            "expect([200, 201]).toContain(response.status());",
            [200, 201],
        ),
    ],
)
def test_extract_http_status_assertions_supports_status_assertion_shapes(
    content: str, expected_codes: list[int]
) -> None:
    assertions = extract_http_status_assertions(
        "tests/notes.test.js",
        "const response = await request(app).post('/api/notes');\n" + content,
    )

    assert assertions
    assert assertions[-1]["expected_status_codes"] == expected_codes


def test_extract_http_status_assertions_ignores_supertest_header_expectation() -> None:
    content = "const response = await request(app).get('/api/notes').expect('Content-Type', /json/);"

    assert extract_http_status_assertions("tests/notes.test.js", content) == []


@pytest.mark.parametrize("status_code", [200, 201, 206])
def test_validate_http_status_contract_accepts_explicit_interface_status(
    tmp_project_dir: Path, status_code: int
) -> None:
    test_path = tmp_project_dir / "e2e" / "notes.spec.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await page.request.post('/api/notes');\n"
        f"expect(response.status()).toBe({status_code});\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "specification": "POST /api/notes returns HTTP status code "
                f"{status_code}.",
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "E2E", "file_path": "e2e/notes.spec.js"}],
    )

    assert diagnostics == []


def test_validate_http_status_contract_uses_interface_output_without_defaulting_to_200(
    tmp_project_dir: Path,
) -> None:
    test_path = tmp_project_dir / "tests" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status()).toBe(201);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "outputs": {"status_code": 201},
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "tests/notes.test.js"}],
    )

    assert diagnostics == []


@pytest.mark.parametrize("response_field", ["outputs", "responses"])
def test_validate_http_status_contract_accepts_arc_output2_auth_shapes(
    tmp_project_dir: Path, response_field: str,
) -> None:
    route_path = tmp_project_dir / "backend" / "src" / "routes" / "auth.routes.js"
    route_path.parent.mkdir(parents=True)
    route_path.write_text(
        "const router = express.Router();\n"
        "router.post('/register', (req, res) => {\n"
        "  // 201 -> { user }\n"
        "  // 400 -> validation error\n"
        "  // 409 -> duplicate email\n"
        "  return res.status(500).json({ code: 'NOT_IMPLEMENTED', message: 'TODO(TDD)' });\n"
        "});\n"
        "router.get('/me', (req, res) => {\n"
        "  // 200 -> { user }\n"
        "  return res.status(500).json({ code: 'NOT_IMPLEMENTED', message: 'TODO(TDD)' });\n"
        "});\n"
        "router.post('/logout', (req, res) => {\n"
        "  // 200 -> { ok: true }\n"
        "  return res.status(500).json({ code: 'NOT_IMPLEMENTED', message: 'TODO(TDD)' });\n"
        "});\n",
        encoding="utf-8",
    )
    test_path = tmp_project_dir / "integration" / "auth.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const register = await request.post('/api/auth/register');\n"
        "expect(register.status).toBe(201);\n"
        "expect(register.status).toBe(400);\n"
        "expect(register.status).toBe(409);\n"
        "const me = await request.get('/api/auth/me');\n"
        "expect(me.status).toBe(200);\n"
        "const logout = await request.post('/api/auth/logout');\n"
        "expect(logout.status).toBe(200);\n",
        encoding="utf-8",
    )
    interface = {
        "interface_id": "IF-AUTH",
        "type": "API",
        "specification": "POST /api/auth/register, GET /api/auth/me, POST /api/auth/logout. "
        "Status codes fixed: register 201 success, 400 field validation, "
        "409 duplicates; me/logout always 200.",
        response_field: {
            "register": "201 { user }; 400 { errors }; 409 { error }",
            "me": "200 { user }",
            "logout": "200 { ok: true }",
        },
        "file_path": "backend/src/routes/auth.routes.js",
    }
    manifest = [{"type": "Integration", "file_path": "integration/auth.test.js"}]

    assert validate_http_status_contracts(
        tmp_project_dir, {"description": "Authentication."}, [interface], manifest
    ) == []

    for field in (response_field, "specification"):
        partial = {**interface}
        partial.pop(field)
        partial["file_path"] = "backend/src/routes/missing.js"
        assert validate_http_status_contracts(
            tmp_project_dir, {"description": "Authentication."}, [partial], manifest
        ) == []


def test_validate_http_status_contract_route_comments_are_scoped_and_placeholder_is_not_a_contract(
    tmp_project_dir: Path,
) -> None:
    route_path = tmp_project_dir / "backend" / "src" / "routes" / "notes.js"
    route_path.parent.mkdir(parents=True)
    route_path.write_text(
        "router.post('/notes', (req, res) => {\n"
        "  // 201 -> created\n"
        "  return res.status(500).json({\n"
        "    code: 'NOT_IMPLEMENTED', message: 'TODO(TDD)'\n"
        "  });\n"
        "});\n"
        "router.get('/users', (req, res) => {\n"
        "  // 200 -> users\n"
        "  return res.status(500).json({ code: 'NOT_IMPLEMENTED' });\n"
        "});\n",
        encoding="utf-8",
    )
    test_path = tmp_project_dir / "integration" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status).toBe(201);\n",
        encoding="utf-8",
    )
    interface = {
        "interface_id": "IF-NOTES",
        "type": "API",
        "specification": "POST /api/notes creates a note.",
        "file_path": "backend/src/routes/notes.js",
    }
    manifest = [{"type": "Integration", "file_path": "integration/notes.test.js"}]

    assert validate_http_status_contracts(tmp_project_dir, {"description": "Create a note."}, [interface], manifest) == []

    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status).toBe(500);\n",
        encoding="utf-8",
    )
    diagnostics = validate_http_status_contracts(
        tmp_project_dir, {"description": "Create a note."}, [interface], manifest
    )
    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "status_code_conflict"
    assert diagnostics[0]["contract_status_codes"] == [201]


@pytest.mark.parametrize(
    "placeholder",
    ["code: 'NOT_IMPLEMENTED'", "message: 'TODO(TDD)'", "message: 'TODO(TDD); work pending'"],
)
def test_validate_http_status_contract_placeholder_only_requires_status_declaration(
    tmp_project_dir: Path, placeholder: str,
) -> None:
    route_path = tmp_project_dir / "backend" / "src" / "routes" / "notes.js"
    route_path.parent.mkdir(parents=True)
    route_path.write_text(
        "router.post('/notes', (req, res) => {\n"
        f"  return res.status(500).json({{ {placeholder} }});\n"
        "});\n",
        encoding="utf-8",
    )
    test_path = tmp_project_dir / "integration" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status).toBe(500);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [{"interface_id": "IF-NOTES", "type": "API", "file_path": "backend/src/routes/notes.js"}],
        [{"type": "Integration", "file_path": "integration/notes.test.js"}],
    )

    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "status_code_needs_info"


def test_validate_http_status_contract_preserves_real_status_next_to_placeholder(tmp_project_dir: Path) -> None:
    route_path = tmp_project_dir / "backend" / "src" / "routes" / "notes.js"
    route_path.parent.mkdir(parents=True)
    route_path.write_text(
        "router.post('/notes', (req, res) => {\n"
        "  if (req.invalid) return res.status(400).json({ error: 'invalid' });\n"
        "  return res.status(500).json({ code: 'NOT_IMPLEMENTED' });\n"
        "});\n",
        encoding="utf-8",
    )
    test_path = tmp_project_dir / "integration" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status).toBe(400);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [{"interface_id": "IF-NOTES", "type": "API", "file_path": "backend/src/routes/notes.js"}],
        [{"type": "Integration", "file_path": "integration/notes.test.js"}],
    )

    assert diagnostics == []


def test_validate_http_status_contract_reads_top_level_status_field(tmp_project_dir: Path) -> None:
    test_path = tmp_project_dir / "tests" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status()).toBe(201);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "status_code": 201,
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "tests/notes.test.js"}],
    )

    assert diagnostics == []


def test_validate_http_status_contract_reads_nested_output_code_field(tmp_project_dir: Path) -> None:
    test_path = tmp_project_dir / "tests" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status()).toBe(201);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "outputs": [{"code": 201, "body": "created"}],
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "tests/notes.test.js"}],
    )

    assert diagnostics == []


def test_validate_http_status_contract_accepts_declared_response_status_set(tmp_project_dir: Path) -> None:
    test_path = tmp_project_dir / "tests" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status()).toBeOneOf([200, 201]);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "outputs": {"status_codes": [200, 201]},
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "tests/notes.test.js"}],
    )

    assert diagnostics == []


def test_validate_http_status_contract_uses_requirement_status_when_interface_is_silent(
    tmp_project_dir: Path,
) -> None:
    test_path = tmp_project_dir / "tests" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status()).toBe(202);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "POST /api/notes returns HTTP status code 202."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "specification": "POST /api/notes creates a note.",
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "tests/notes.test.js"}],
    )

    assert diagnostics == []


def test_validate_http_status_contract_reads_explicit_route_status(tmp_project_dir: Path) -> None:
    route_path = tmp_project_dir / "backend" / "src" / "routes" / "notes.js"
    route_path.parent.mkdir(parents=True)
    route_path.write_text(
        "const router = express.Router();\n"
        "router.post('/notes', (req, res) => res.status(206).json({}));\n",
        encoding="utf-8",
    )
    test_path = tmp_project_dir / "e2e" / "notes.spec.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await page.request.post('/api/notes');\n"
        "expect(response.status()).toBe(206);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "specification": "POST /api/notes",
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "E2E", "file_path": "e2e/notes.spec.js"}],
    )

    assert diagnostics == []


def test_validate_http_status_contract_does_not_mix_statuses_from_sibling_routes(
    tmp_project_dir: Path,
) -> None:
    route_path = tmp_project_dir / "backend" / "src" / "routes" / "notes.js"
    route_path.parent.mkdir(parents=True)
    route_path.write_text(
        "const router = express.Router();\n"
        "router.get('/users', (req, res) => res.status(200).json({}));\n"
        "router.post('/notes', (req, res) => res.status(201).json({}));\n",
        encoding="utf-8",
    )
    test_path = tmp_project_dir / "integration" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request(app).post('/api/notes');\n"
        "expect(response.status()).toBe(200);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "specification": "POST /api/notes creates a note.",
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "integration/notes.test.js"}],
    )

    assert diagnostics[0]["code"] == "status_code_conflict"
    assert diagnostics[0]["contract_status_codes"] == [201]


def test_validate_http_status_contract_rejects_requirement_interface_conflict(
    tmp_project_dir: Path,
) -> None:
    test_path = tmp_project_dir / "integration" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status()).toBe(201);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "POST /api/notes returns 201."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "specification": "POST /api/notes returns 200.",
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "integration/notes.test.js"}],
    )

    assert diagnostics
    assert diagnostics[0]["code"] == "status_code_conflict"
    assert "201" in diagnostics[0]["message"]
    assert "200" in diagnostics[0]["message"]


def test_validate_http_status_contract_rejects_conflicting_assertion_with_diagnostic(
    tmp_project_dir: Path,
) -> None:
    test_path = tmp_project_dir / "e2e" / "notes.spec.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await page.request.post('/api/notes');\n"
        "expect(response.status()).toBe(200);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "specification": "POST /api/notes returns 201.",
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "E2E", "file_path": "e2e/notes.spec.js"}],
    )

    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic["code"] == "status_code_conflict"
    assert diagnostic["file_path"] == "e2e/notes.spec.js"
    assert diagnostic["assertion"] == "expect(response.status()).toBe(200)"
    assert diagnostic["expected_status_codes"] == [200]
    assert diagnostic["contract_status_codes"] == [201]
    assert diagnostic["interface_id"] == "IF-NOTES"
    assert "201" in diagnostic["message"]


def test_validate_http_status_contract_reports_needs_info_without_status_source(
    tmp_project_dir: Path,
) -> None:
    test_path = tmp_project_dir / "integration" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status()).toBe(200);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "specification": "POST /api/notes creates a note.",
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "integration/notes.test.js"}],
    )

    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "status_code_needs_info"
    assert "needs-info" in diagnostics[0]["message"]
    assert "not enough HTTP status" in diagnostics[0]["message"]


def test_validate_http_status_contract_rejects_unbounded_2xx_matcher(tmp_project_dir: Path) -> None:
    test_path = tmp_project_dir / "integration" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status()).toBeGreaterThanOrEqual(200);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "specification": "POST /api/notes returns 201.",
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "integration/notes.test.js"}],
    )

    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "status_code_needs_info"
    assert "specific status code" in diagnostics[0]["message"]


def test_validate_http_status_contract_accepts_route_table_arrow_spec(tmp_project_dir: Path) -> None:
    """The 2026-09-25 easy-ticketbooking run died on exactly this shape.

    DESIGN recorded statuses in `specification` prose as route-table arrows
    (`POST /api/auth/register -> 201 ; 400 { errors }`), which the gate's
    text patterns could not read, so every status assertion became
    needs-info and the whole compile was rejected with no repair round.
    """

    test_path = tmp_project_dir / "tests" / "auth.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const register = await request.post('/api/auth/register');\n"
        "expect(register.status).toBe(201);\n"
        "expect(register.status).toBe(400);\n"
        "expect(register.status).toBe(409);\n"
        "const session = await request.get('/api/auth/session');\n"
        "expect(session.status).toBe(200);\n"
        "const logout = await request.post('/api/auth/logout');\n"
        "expect(logout.status).toBe(200);\n",
        encoding="utf-8",
    )
    interface = {
        "interface_id": "REQ-1-API-Auth",
        "type": "API",
        "specification": (
            "POST /api/auth/register -> 201 { user:{id,username} } on success; "
            "400 { errors:{field:code} } for REQUIRED/FORMAT; 409 { errors:{username:'DUPLICATE_USERNAME'} }. "
            "GET /api/auth/session -> 200 { user:{id,username}|null }. "
            "POST /api/auth/logout -> 200 { ok:true }."
        ),
        "file_path": "backend/src/routes/auth.js",
    }

    assert validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Register a traveler account."},
        [interface],
        [{"type": "Integration", "file_path": "tests/auth.test.js"}],
    ) == []


def test_validate_http_status_contract_reads_leading_route_table_comment(tmp_project_dir: Path) -> None:
    """Skeleton route tables put the contract comment ABOVE each declaration.

    Per-declaration segments start at the declaration, so a leading comment
    table belonged to no segment and its declared statuses were invisible;
    only the TODO(TDD) scaffold responses (correctly ignored) remained.
    """

    route_path = tmp_project_dir / "backend" / "src" / "routes" / "auth.js"
    route_path.parent.mkdir(parents=True)
    route_path.write_text(
        "const router = express.Router();\n"
        "\n"
        "// POST /api/auth/register -> 201 { user: { id, username } };\n"
        "//   400 { errors: { <field>: <code> } } for REQUIRED/FORMAT;\n"
        "//   409 { errors: { username: 'DUPLICATE_USERNAME' } }.\n"
        "router.post('/register', (req, res) => {\n"
        "  void req; void res;\n"
        "  res.status(501).json({ message: 'TODO(TDD)' });\n"
        "});\n"
        "\n"
        "// GET /api/auth/session -> 200 { user: { id, username } | null }.\n"
        "router.get('/session', (req, res) => {\n"
        "  res.status(501).json({ message: 'TODO(TDD)' });\n"
        "});\n"
        "\n"
        "// POST /api/auth/logout -> 200 { ok: true }.\n"
        "router.post('/logout', (req, res) => {\n"
        "  res.status(501).json({ message: 'TODO(TDD)' });\n"
        "});\n",
        encoding="utf-8",
    )
    test_path = tmp_project_dir / "tests" / "auth.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const register = await request.post('/api/auth/register');\n"
        "expect(register.status).toBe(201);\n"
        "expect(register.status).toBe(400);\n"
        "expect(register.status).toBe(409);\n"
        "const session = await request.get('/api/auth/session');\n"
        "expect(session.status).toBe(200);\n"
        "const logout = await request.post('/api/auth/logout');\n"
        "expect(logout.status).toBe(200);\n",
        encoding="utf-8",
    )
    interface = {
        "interface_id": "REQ-1-API-Auth",
        "type": "API",
        # Deliberately status-free prose: the route source must be the
        # only recognized declaration source in this test.
        "specification": "Auth route boundary for register, session and logout.",
        "file_path": "backend/src/routes/auth.js",
    }

    assert validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Register a traveler account."},
        [interface],
        [{"type": "Integration", "file_path": "tests/auth.test.js"}],
    ) == []


def test_validate_http_status_contract_ignores_arrow_without_route_path(tmp_project_dir: Path) -> None:
    """The arrow notation is anchored on a `/`-leading path token.

    A prose arrow after a plain word or number must not register a status
    code, otherwise guessed assertions would pass without any contract.
    """

    test_path = tmp_project_dir / "tests" / "notes.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const response = await request.post('/api/notes');\n"
        "expect(response.status).toBe(201);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a note."},
        [
            {
                "interface_id": "IF-NOTES",
                "type": "API",
                "specification": "See step 2 -> 201 in the flow diagram; latency budget 128 { ms } per call.",
                "file_path": "backend/src/routes/notes.js",
            }
        ],
        [{"type": "Integration", "file_path": "tests/notes.test.js"}],
    )

    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "status_code_needs_info"


def test_path_matches_parameterized_routes() -> None:
    """Express ``:param``/``*`` segments match one non-empty concrete segment.

    The 2026-09-25 hackathon-sheet REQ-1-1-1 card declared ``GET
    /api/workbooks/:id/state`` while the generated test drove
    ``/api/workbooks/q3-sales/state``; literal-only matching reported "no
    matching API interface" for every such assertion.
    """

    assert _path_matches("/api/workbooks/:id/state", "/api/workbooks/q3-sales/state")
    # Router-relative declarations align to the tail of the requested path,
    # mirroring the suffix semantics of the literal match.
    assert _path_matches("/:id/state", "/api/workbooks/q3-sales/state")
    assert _path_matches("/api/*/state", "/api/workbooks/state")
    # Literal equality and suffix semantics are preserved.
    assert _path_matches("/api/workbooks", "/api/workbooks")
    assert _path_matches("/register", "/api/auth/register")
    assert not _path_matches("/api/workbooks/:id/state", "/api/workbooks")
    assert not _path_matches("/api/workbooks/:id/state", "/api/workbooks/q3-sales")
    assert not _path_matches("/:id", "/")


def test_path_matches_relative_dynamic_route_does_not_reinterpret_static_tail() -> None:
    """A relative ``/:id`` route must not swallow a static route tail.

    The 2026-09-26 hackathon-sheet REQ-1-1-1 run matched the router-relative
    detail declaration ``router.get('/:id')`` (skeleton status 404) against
    the list assertion path ``GET /api/workbooks``, manufacturing a
    "interface declares 200, but the matched route declares 404" conflict on
    an assertion the contract fully allowed.
    """

    assert not _path_matches("/:id", "/api/workbooks")
    assert not _path_matches("/:id", "/api/workbooks/")
    # The router-relative detail route still owns concrete ids under its
    # mount, and deeper parameterized shapes are unaffected.
    assert _path_matches("/:id", "/api/workbooks/123")
    assert _path_matches("/:id/state", "/api/workbooks/q3-sales/state")


def test_route_status_declarations_parse_pipe_separated_statuses() -> None:
    """DESIGN cards connect alternative response statuses with ``|``.

    The 2026-09-26 hackathon-sheet card wrote ``-> 200 detail | 404
    WORKBOOK_NOT_FOUND`` and ``-> 200 { ok: true } | 404 { error } | 400
    { error } ; 500 { error }``; the parser kept only the arrow status and
    the ``; {`` continuation, so the detail 404 and the PUT 400 vanished and
    every assertion on them was rejected as a conflict.
    """

    specification = (
        "Added POST /api/workbooks -> 201 { id, name } ; "
        "400 { error: 'WORKBOOK_NAME_REQUIRED' } ; 500 { error }. "
        "Existing GET /api/workbooks -> 200 { workbooks } ; "
        "GET /api/workbooks/:id -> 200 detail | 404 WORKBOOK_NOT_FOUND unchanged. "
        "PUT /api/workbooks/:id/worksheets/:worksheetId/cells/:rowIndex/:colIndex "
        "body { value } -> 200 { ok: true } | 404 { error: 'WORKBOOK_NOT_FOUND' } "
        "| 400 { error: 'INVALID_VALUE' } ; 500 { error }. Non-numeric ids -> 404."
    )

    routes = {
        (route["method"], route["path"]): route["status_codes"]
        for route in _route_status_declarations(specification)
    }

    assert routes[("GET", "/api/workbooks")] == [200]
    assert routes[("GET", "/api/workbooks/:id")] == [200, 404]
    assert set(routes[("POST", "/api/workbooks")]) == {201, 400, 500}
    assert set(
        routes[
            ("PUT", "/api/workbooks/:id/worksheets/:worksheetId/cells/:rowIndex/:colIndex")
        ]
    ) == {200, 404, 400, 500}


def test_validate_http_status_contract_parameterized_route_card_matches_concrete_paths(
    tmp_project_dir: Path,
) -> None:
    """A card declaring a parameterized route owns assertions on concrete ids.

    The sibling users card keeps the assertion away from the single-card
    fallback, so this test only passes when the parameterized path actually
    matches.
    """

    test_path = tmp_project_dir / "tests" / "workbooks.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const state = await request.get('/api/workbooks/q3-sales/state');\n"
        "expect(state.status).toBe(200);\n"
        "const missing = await request.get('/api/workbooks/unknown-id/state');\n"
        "expect(missing.status).toBe(404);\n",
        encoding="utf-8",
    )

    assert validate_http_status_contracts(
        tmp_project_dir,
        {"description": "View and open a workbook."},
        [
            {
                "interface_id": "REQ-1-1-1-API-Workbooks",
                "type": "API",
                "specification": (
                    "GET /api/workbooks -> 200 { workbooks } ; "
                    "GET /api/workbooks/:id/state -> 200 { workbook } ; 404 { error }."
                ),
                "outputs": {"status_codes": [200, 404]},
                "file_path": "backend/src/routes/workbooks.js",
            },
            {
                "interface_id": "IF-USERS",
                "type": "API",
                "specification": "GET /api/users -> 200 { users }.",
                "file_path": "backend/src/routes/users.js",
            },
        ],
        [{"type": "Integration", "file_path": "tests/workbooks.test.js"}],
    ) == []


def test_validate_http_status_contract_bare_mount_path_does_not_compete_for_method_assertions(
    tmp_project_dir: Path,
) -> None:
    """hackathon-sheet REQ-1-1-1: the mount card's only path is the quoted
    ``app.use('/api/workbooks', ...)`` extraction, which carries no method.

    A no-method bare path must not make the router card's method-bearing
    assertions ambiguous: when a candidate matches with the assertion's
    method, bare-path-only candidates step out of the ownership contest.
    """

    test_path = tmp_project_dir / "tests" / "workbooks.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const res = await request.get('/api/workbooks');\n"
        "expect(res.status).toBe(200);\n",
        encoding="utf-8",
    )

    assert validate_http_status_contracts(
        tmp_project_dir,
        {"description": "View available workbooks."},
        [
            {
                "interface_id": "REQ-1-1-1-API-Workbooks",
                "type": "API",
                "specification": "GET /api/workbooks -> 200 { workbooks }.",
                "outputs": {"status_codes": [200]},
                "file_path": "backend/src/routes/workbooks.js",
            },
            {
                "interface_id": "REQ-1-1-1-API-AppMount",
                "type": "API",
                "specification": (
                    "Add `app.use('/api/workbooks', require('./routes/workbooks'))` "
                    "at the established mount point."
                ),
                "file_path": "backend/src/app.js",
            },
        ],
        [{"type": "Integration", "file_path": "tests/workbooks.test.js"}],
    ) == []


def test_validate_http_status_contract_same_status_set_candidates_are_interchangeable(
    tmp_project_dir: Path,
) -> None:
    """Same-path candidates declaring identical status sets are interchangeable.

    Picking among them cannot change the verdict, so the gate proceeds; the
    conflict diagnostic names the card the test manifest declared.
    """

    test_path = tmp_project_dir / "tests" / "things.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const res = await request.post('/api/things');\n"
        "expect(res.status).toBe(201);\n"
        "expect(res.status).toBe(500);\n",
        encoding="utf-8",
    )
    cards = [
        {
            "interface_id": interface_id,
            "type": "API",
            "specification": "POST /api/things -> 201 { thing } ; 400 { errors }.",
            "outputs": {"status_codes": [201, 400]},
            "file_path": "backend/src/routes/things.js",
        }
        for interface_id in ("IF-THINGS-A", "IF-THINGS-B")
    ]

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a thing."},
        cards,
        [
            {
                "type": "Integration",
                "file_path": "tests/things.test.js",
                "interface_ids": ["IF-THINGS-B"],
            }
        ],
    )

    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "status_code_conflict"
    assert diagnostics[0]["interface_id"] == "IF-THINGS-B"
    assert diagnostics[0]["contract_status_codes"] == [201, 400]


def test_validate_http_status_contract_inconsistent_candidate_status_sets_stay_ambiguous(
    tmp_project_dir: Path,
) -> None:
    """Disambiguation must not weaken conflict detection.

    Same-path candidates whose contract status sets differ keep the
    ambiguity needs-info: picking either one could flip the verdict.
    """

    test_path = tmp_project_dir / "tests" / "things.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const res = await request.post('/api/things');\n"
        "expect(res.status).toBe(201);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a thing."},
        [
            {
                "interface_id": "IF-THINGS-A",
                "type": "API",
                "specification": "POST /api/things returns 201.",
                "file_path": "backend/src/routes/thingsA.js",
            },
            {
                "interface_id": "IF-THINGS-B",
                "type": "API",
                "specification": "POST /api/things returns 409.",
                "file_path": "backend/src/routes/thingsB.js",
            },
        ],
        [{"type": "Integration", "file_path": "tests/things.test.js"}],
    )

    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "status_code_needs_info"
    assert "more than one API interface" in diagnostics[0]["message"]


def test_validate_http_status_contract_prefers_full_path_over_mislabeled_func_card(
    tmp_project_dir: Path,
) -> None:
    """A relative path on a mis-typed FUNC card must not shadow the API card.

    The 2026-09-26 hackathon-sheet REQ-1-2-1 replay contained a service
    boundary card marked ``type=API`` with ``POST /workbooks``.  Its suffix
    match competed with the real ``POST /api/workbooks`` card and produced a
    needs-info diagnostic even though the API card declared both statuses.
    The full mounted path is the more specific ownership candidate.
    """

    test_path = tmp_project_dir / "integration" / "workbooks-create.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const created = await request.post('/api/workbooks');\n"
        "expect(created.status).toBe(201);\n"
        "const invalid = await request.post('/api/workbooks');\n"
        "expect(invalid.status).toBe(400);\n",
        encoding="utf-8",
    )

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a blank workbook."},
        [
            {
                "interface_id": "REQ-1-1-1-API-Workbooks",
                "type": "API",
                "specification": (
                    "POST /api/workbooks -> 201 { id, name } | "
                    "400 { error: 'WORKBOOK_NAME_REQUIRED' }."
                ),
                "outputs": {"status_codes": [201, 400]},
                "file_path": "backend/src/routes/workbooks.js",
            },
            {
                "interface_id": "REQ-1-1-1-FUNC-Workbooks",
                # This is the production failure shape: a FUNC service card
                # was incorrectly persisted as API and therefore entered the
                # HTTP status candidate pool.
                "type": "API",
                "specification": (
                    "createWorkbook(name): POST /workbooks { name } -> 201; "
                    "errors are raised through the shared apiClient."
                ),
                "file_path": "backend/src/services/workbooks.js",
            },
        ],
        [
            {
                "type": "Integration",
                "file_path": "integration/workbooks-create.test.js",
                "interface_ids": ["REQ-1-1-1-API-Workbooks"],
            }
        ],
    )

    assert diagnostics == []


def test_validate_http_status_contract_accepts_hackathon_sheet_workbooks_shape(
    tmp_project_dir: Path,
) -> None:
    """Inline replay of the 2026-09-25 hackathon-sheet REQ-1-1-1 rejection.

    The gate reported three needs-info diagnostics on this exact shape —
    param-route assertions matched no interface, and the router + mount
    cards made GET /api/workbooks ambiguous — although every assertion
    matched the declared contract exactly.
    """

    routes_path = tmp_project_dir / "backend" / "src" / "routes" / "workbooks.js"
    routes_path.parent.mkdir(parents=True)
    routes_path.write_text(
        "const express = require('express');\n"
        "const router = express.Router();\n"
        "\n"
        "// REQ-1-1-1 workbook API.\n"
        "// GET /api/workbooks            -> 200 { workbooks: [...] }\n"
        "// GET /api/workbooks/:id/state  -> 200 { workbook, worksheets }\n"
        "//                               -> 404 { error: 'WORKBOOK_NOT_FOUND' }\n"
        "router.get('/', (req, res) => {\n"
        "  res.status(200).json({ workbooks: [] });\n"
        "});\n"
        "\n"
        "router.get('/:id/state', (req, res) => {\n"
        "  res.status(404).json({ error: 'WORKBOOK_NOT_FOUND' });\n"
        "});\n"
        "\n"
        "module.exports = router;\n",
        encoding="utf-8",
    )
    app_path = tmp_project_dir / "backend" / "src" / "app.js"
    app_path.write_text(
        "const express = require('express');\n"
        "const app = express();\n"
        "app.get('/api/health', (req, res) => {\n"
        "  res.json({ code: 200, message: 'Backend Ready' });\n"
        "});\n"
        "app.get('/', (req, res) => {\n"
        "  res.status(503).type('html').send('frontend build missing');\n"
        "});\n"
        "module.exports = app;\n",
        encoding="utf-8",
    )
    test_path = (
        tmp_project_dir / "backend" / "tests" / "generated" / "req_1_1_1" / "workbooks_api.test.js"
    )
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const res = await request.get('/api/workbooks');\n"
        "expect(res.status).toBe(200);\n"
        "const state = await request.get('/api/workbooks/q3-sales/state');\n"
        "expect(state.status).toBe(200);\n"
        "const missing = await request.get('/api/workbooks/unknown-id/state');\n"
        "expect(missing.status).toBe(404);\n",
        encoding="utf-8",
    )
    interfaces = [
        {
            "interface_id": "REQ-1-1-1-API-Workbooks",
            "type": "API",
            "specification": (
                "GET /api/workbooks -> 200 { workbooks } ; "
                "GET /api/workbooks/:id/state -> 200 { workbook, worksheets } ; "
                "404 { error: 'WORKBOOK_NOT_FOUND' }. Mounted at /api/workbooks in app.js."
            ),
            "outputs": {"status_codes": [200, 404]},
            "file_path": "backend/src/routes/workbooks.js",
        },
        {
            "interface_id": "REQ-1-1-1-API-AppMount",
            "type": "API",
            "specification": (
                "Add `app.use('/api/workbooks', require('./routes/workbooks'))` "
                "at the established mount point (after /api/health)."
            ),
            "file_path": "backend/src/app.js",
        },
    ]
    manifest = [
        {
            "test_id": "REQ-1-1-1-INT-WorkbooksApi",
            "req_id": "REQ-1-1-1",
            "interface_ids": ["REQ-1-1-1-API-Workbooks", "REQ-1-1-1-API-AppMount"],
            "type": "Integration",
            "file_path": "backend/tests/generated/req_1_1_1/workbooks_api.test.js",
        }
    ]

    assert validate_http_status_contracts(
        tmp_project_dir,
        {"description": "View and open a workbook."},
        interfaces,
        manifest,
    ) == []


def test_validate_http_status_contract_accepts_or_connected_status_clauses(
    tmp_project_dir: Path,
) -> None:
    """Interface prose may connect alternative response clauses with ``or``.

    The 2026-09-26 hackathon-sheet card used ``200 ... or 404 ...`` and
    ``200 ... or 400 ... or 404 ...``.  The status gate must retain those
    alternatives instead of reducing the interface-wide contract to only the
    first arrow status.
    """

    routes_path = tmp_project_dir / "backend" / "src" / "routes" / "workbooks.js"
    routes_path.parent.mkdir(parents=True)
    routes_path.write_text(
        "const router = require('express').Router();\n"
        "router.get('/workbooks', (req, res) => {\n"
        "  res.status(501).json({ code: 'NOT_IMPLEMENTED' });\n"
        "});\n"
        "router.get('/workbooks/:id', (req, res) => {\n"
        "  res.status(501).json({ code: 'NOT_IMPLEMENTED' });\n"
        "});\n"
        "router.put('/workbooks/:id/worksheets/:worksheetName/cells', (req, res) => {\n"
        "  res.status(501).json({ code: 'NOT_IMPLEMENTED' });\n"
        "});\n",
        encoding="utf-8",
    )
    test_path = tmp_project_dir / "integration" / "workbooks.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const list = await request.get('/api/workbooks');\n"
        "expect(list.status).toBe(200);\n"
        "const detail = await request.get('/api/workbooks/wb_q3_sales');\n"
        "expect(detail.status).toBe(200);\n"
        "const missing = await request.get('/api/workbooks/missing-workbook');\n"
        "expect(missing.status).toBe(404);\n"
        "const update = await request.put('/api/workbooks/wb_q3_sales/worksheets/Sheet1/cells');\n"
        "expect(update.status).toBe(200);\n"
        "const invalid = await request.put('/api/workbooks/wb_q3_sales/worksheets/Sheet1/cells');\n"
        "expect(invalid.status).toBe(400);\n"
        "const unknown = await request.put('/api/workbooks/unknown/worksheets/Sheet1/cells');\n"
        "expect(unknown.status).toBe(404);\n",
        encoding="utf-8",
    )
    interfaces = [
        {
            "interface_id": "REQ-1-1-1-API-Workbooks",
            "type": "API",
            "specification": (
                "Mounted via app.use('/api', workbookRoutes). Routes and exact statuses: "
                "GET /api/workbooks -> 200 {workbooks:[{id,name,lastUpdatedAt}]}; "
                "GET /api/workbooks/:id -> 200 {workbook,activeWorksheetId,worksheets,cells,filterViews,validations,pivotTables} "
                "or 404 {code:WORKBOOK_NOT_FOUND}; "
                "PUT /api/workbooks/:id/worksheets/:worksheetName/cells body {updates:[{row,col,value}]} "
                "-> 200 updated detail or 400 {code:VALIDATION_FAILED|CELL_OUT_OF_RANGE} "
                "or 404 {code:WORKBOOK_NOT_FOUND|WORKSHEET_NOT_FOUND}."
            ),
            "file_path": "backend/src/routes/workbooks.js",
        }
    ]
    manifest = [
        {
            "test_id": "REQ-1-1-1-INT-WorkbooksApi",
            "req_id": "REQ-1-1-1",
            "interface_ids": ["REQ-1-1-1-API-Workbooks"],
            "type": "Integration",
            "file_path": "integration/workbooks.test.js",
        }
    ]

    assert validate_http_status_contracts(
        tmp_project_dir,
        {"description": "View and open a workbook."},
        interfaces,
        manifest,
    ) == []


def test_validate_http_status_contract_accepts_hackathon_sheet_create_shape(
    tmp_project_dir: Path,
) -> None:
    """Inline replay of the hackathon-sheet REQ-1-2-1 rejection.

    The reused+updated router card and the reused mount card made every
    POST /api/workbooks assertion ambiguous although the router card's
    declared codes (201/400/409) matched each assertion exactly.
    """

    routes_path = tmp_project_dir / "backend" / "src" / "routes" / "workbooks.js"
    routes_path.parent.mkdir(parents=True)
    routes_path.write_text(
        "const express = require('express');\n"
        "const router = express.Router();\n"
        "\n"
        "// REQ-1-1-1 workbook API + REQ-1-2-1 creation.\n"
        "// POST /api/workbooks -> 201 { workbook: { id, name } }\n"
        "//                                -> 400 { error: 'VALIDATION_ERROR' }\n"
        "//                                -> 409 { error: 'DUPLICATE_WORKBOOK_NAME' }\n"
        "// GET  /api/workbooks -> 200 { workbooks }\n"
        "router.post('/', (req, res) => {\n"
        "  res.status(501).json({ error: 'NOT_IMPLEMENTED' });\n"
        "});\n"
        "router.get('/', (req, res) => {\n"
        "  res.status(200).json({ workbooks: [] });\n"
        "});\n"
        "\n"
        "module.exports = router;\n",
        encoding="utf-8",
    )
    test_path = (
        tmp_project_dir / "backend" / "tests" / "generated" / "req_1_2_1" / "workbooksCreateApi.test.js"
    )
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const res = await request.post('/api/workbooks');\n"
        "expect(res.status).toBe(201);\n"
        "expect(res.status).toBe(400);\n"
        "expect(res.status).toBe(409);\n",
        encoding="utf-8",
    )
    interfaces = [
        {
            "interface_id": "REQ-1-1-1-API-Workbooks",
            "type": "API",
            "specification": (
                "POST /api/workbooks -> 201 { workbook: { id, name } } ; "
                "400 { error: 'VALIDATION_ERROR' } ; 409 { error: 'DUPLICATE_WORKBOOK_NAME' }. "
                "GET routes unchanged."
            ),
            "outputs": "status_codes [201, 400, 409]",
            "file_path": "backend/src/routes/workbooks.js",
        },
        {
            "interface_id": "REQ-1-1-1-API-AppMount",
            "type": "API",
            "specification": (
                "Unchanged: app.use('/api/workbooks', ...) already mounts the router "
                "that now owns POST creation."
            ),
            "file_path": "backend/src/app.js",
        },
    ]
    manifest = [
        {
            "test_id": "REQ-1-2-1-INT-WorkbooksCreateApi",
            "req_id": "REQ-1-2-1",
            "interface_ids": ["REQ-1-1-1-API-Workbooks"],
            "type": "Integration",
            "file_path": "backend/tests/generated/req_1_2_1/workbooksCreateApi.test.js",
        }
    ]

    assert validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Create a blank workbook."},
        interfaces,
        manifest,
    ) == []


def test_workbook_routes_keep_comma_statuses_and_template_get_separate(
    tmp_project_dir: Path,
) -> None:
    test_path = tmp_project_dir / "tests" / "workbooks.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const created = await request.post('/api/workbooks');\n"
        "expect(created.status).toBe(201);\n"
        "expect(created.status).toBe(400);\n"
        "expect(created.status).toBe(500);\n"
        "const id = 'missing';\n"
        "const found = await request.get(`/api/workbooks/${id}`);\n"
        "expect(found.status).toBe(200);\n"
        "expect(found.status).toBe(404);\n"
        "const updated = await request.patch(`/api/workbooks/${id}`);\n"
        "expect(updated.status).toBe(200);\n"
        "expect(updated.status).toBe(400);\n"
        "expect(updated.status).toBe(404);\n"
        "expect(updated.status).toBe(409);\n",
        encoding="utf-8",
    )
    card = {
        "interface_id": "REQ-1-1-1-API-Workbooks",
        "type": "API",
        "specification": (
            "GET /api/workbooks/:id returns 200 {workbook}, 404 {notFound}. "
            "PATCH /api/workbooks/:id returns 200 {workbook}, 400 {invalid}, "
            "404 {missing}, 409 {conflict}. "
            "POST /api/workbooks returns 201 {created}, 400 {invalid}, 500 {failure}."
        ),
        "file_path": "backend/src/routes/workbooks.js",
    }
    tests = [{"type": "Integration", "file_path": "tests/workbooks.test.js",
              "interface_ids": [card["interface_id"]]}]
    assertions = extract_http_status_assertions("tests/workbooks.test.js", test_path.read_text())
    assert {(item["method"], item["path"]) for item in assertions} == {
        ("POST", "/api/workbooks"),
        ("GET", "/api/workbooks/${id}"),
        ("PATCH", "/api/workbooks/${id}"),
    }
    assert validate_http_status_contracts(tmp_project_dir, {}, [card], tests) == []

    test_path.write_text(
        "const id = 'missing';\n"
        "const found = await request.get(`/api/workbooks/${id}`);\n"
        "expect(found.status).toBe(201);\n", encoding="utf-8",
    )
    diagnostics = validate_http_status_contracts(tmp_project_dir, {}, [card], tests)
    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "status_code_conflict"
    assert diagnostics[0]["contract_status_codes"] == [200, 404]

    tests[0]["interface_ids"] = ["IF-IMPORT-UI"]
    test_path.write_text(
        "const found = await request.get(`/api/workbooks/${id}`);\n"
        "expect(found.status).toBe(200);\n", encoding="utf-8",
    )
    diagnostics = validate_http_status_contracts(tmp_project_dir, {}, [card], tests)
    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "status_code_needs_info"
    assert card["interface_id"] in diagnostics[0]["message"]


def test_import_route_comment_supplies_only_its_own_declared_statuses(tmp_project_dir: Path) -> None:
    route = tmp_project_dir / "backend/src/routes/workbooks.js"
    route.parent.mkdir(parents=True)
    route.write_text(
        "const router = require('express').Router();\n"
        "// GET /api/workbooks -> 200 {workbooks}\n"
        "router.get('/', (req, res) => res.status(501).json({code: 'NOT_IMPLEMENTED'}));\n"
        "// POST /api/workbooks/import -> 201 {workbook}, 400 {errors}\n"
        "router.post('/import', (req, res) => res.status(501).json({code: 'NOT_IMPLEMENTED'}));\n",
        encoding="utf-8",
    )
    test = tmp_project_dir / "tests/import.test.js"
    test.parent.mkdir(parents=True)
    test.write_text(
        "const response = await request.post('/api/workbooks/import');\n"
        "expect(response.status).toBe(201);\n"
        "expect(response.status).toBe(400);\n", encoding="utf-8",
    )
    card = {"interface_id": "IF-IMPORT", "type": "API",
            "file_path": "backend/src/routes/workbooks.js",
            "specification": "POST /api/workbooks/import accepts CSV."}
    manifest = [{"type": "Integration", "file_path": "tests/import.test.js",
                 "interface_ids": ["IF-IMPORT"]}]
    assert validate_http_status_contracts(tmp_project_dir, {}, [card], manifest) == []
    test.write_text(
        "const response = await request.post('/api/workbooks/import');\n"
        "expect(response.status).toBe(200);\n", encoding="utf-8",
    )
    diagnostics = validate_http_status_contracts(tmp_project_dir, {}, [card], manifest)
    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "status_code_conflict"
    assert diagnostics[0]["contract_status_codes"] == [201, 400]


def test_find_unregistered_api_routes_ignores_unchanged_baseline_routes(
    tmp_project_dir: Path,
) -> None:
    app_path = tmp_project_dir / "backend/src/app.js"
    app_path.parent.mkdir(parents=True)
    baseline = (
        "app.get('/api/health', (req, res) => {\n"
        "  res.json({ code: 200, message: 'Backend Ready' });\n"
        "});\n"
    )
    app_path.write_text(
        baseline
        + "app.get('/api/workbooks', (req, res) => {\n"
        + "  res.json({ workbooks: [] });\n"
        + "});\n",
        encoding="utf-8",
    )

    missing = find_unregistered_api_routes(
        tmp_project_dir,
        ["backend/src/app.js"],
        [
            {
                "interface_id": "IF-WORKBOOKS",
                "type": "API",
                "specification": "GET /api/workbooks -> 200 { workbooks }.",
                "file_path": "backend/src/app.js",
            }
        ],
        baseline_contents={"backend/src/app.js": baseline},
    )

    assert missing == []


def test_find_unregistered_api_routes_still_rejects_new_unregistered_routes(
    tmp_project_dir: Path,
) -> None:
    app_path = tmp_project_dir / "backend/src/app.js"
    app_path.parent.mkdir(parents=True)
    baseline = (
        "app.get('/api/health', (req, res) => {\n"
        "  res.json({ code: 200, message: 'Backend Ready' });\n"
        "});\n"
    )
    app_path.write_text(
        baseline
        + "app.get('/api/workbooks', (req, res) => {\n"
        + "  res.json({ workbooks: [] });\n"
        + "});\n",
        encoding="utf-8",
    )

    missing = find_unregistered_api_routes(
        tmp_project_dir,
        ["backend/src/app.js"],
        [],
        baseline_contents={"backend/src/app.js": baseline},
    )

    assert missing == [
        {"file_path": "backend/src/app.js", "method": "GET", "path": "/api/workbooks"}
    ]


HACKATHON_WORKBOOKS_SKELETON = """\
const express = require('express');
const { listWorkbooks, getWorkbookDetail, setCell } = require('../services/workbook_service');

const router = express.Router();

// Route table (statuses are the API contract):
// GET  /api/workbooks -> 200 { workbooks: [...] } ; 500 { error }
// GET  /api/workbooks/:id -> 200 WorkbookDetail ; 404 { error: 'WORKBOOK_NOT_FOUND' } ; 500 { error }
// PUT  /api/workbooks/:id/worksheets/:worksheetId/cells/:rowIndex/:colIndex
//      body { value } -> 200 { ok: true } ; 400 { error: 'INVALID_VALUE' } ;
//      404 { error: 'WORKBOOK_NOT_FOUND' } ; 500 { error }
// TODO(TDD): implement the three handlers mapping service results/errors to
// the statuses above; workbook ids are integers, non-numeric -> 404.

router.get('/', async (req, res) => {
  // TODO(TDD): 200 { workbooks }
  res.json({ workbooks: await listWorkbooks() });
});

router.get('/:id', async (req, res) => {
  // TODO(TDD): 200 detail | 404 WORKBOOK_NOT_FOUND
  const detail = await getWorkbookDetail(Number(req.params.id));
  if (!detail) {
    res.status(404).json({ error: 'WORKBOOK_NOT_FOUND' });
    return;
  }
  res.json(detail);
});

router.put('/:id/worksheets/:worksheetId/cells/:rowIndex/:colIndex', async (req, res) => {
  // TODO(TDD): 200 { ok: true } | 404 WORKBOOK_NOT_FOUND
  await setCell({
    workbookId: Number(req.params.id),
    worksheetId: Number(req.params.worksheetId),
    rowIndex: Number(req.params.rowIndex),
    colIndex: Number(req.params.colIndex),
    value: req.body?.value,
  });
  res.json({ ok: true });
});

module.exports = router;
"""

HACKATHON_WORKBOOKS_INTEGRATION = """\
const res = await request(app).get('/api/workbooks');
expect(res.status).toBe(200);
const detail = await request(app).get('/api/workbooks/1');
expect(detail.status).toBe(200);
const missing = await request(app).get('/api/workbooks/9999');
expect(missing.status).toBe(404);
const nonNumeric = await request(app).get('/api/workbooks/not-a-number');
expect(nonNumeric.status).toBe(404);
const updated = await request(app)
  .put('/api/workbooks/1/worksheets/s1/cells/1/1')
  .send({ value: '1200' });
expect(updated.status).toBe(200);
const unknownSheet = await request(app)
  .put('/api/workbooks/1/worksheets/9999/cells/0/0')
  .send({ value: 'x' });
expect(unknownSheet.status).toBe(404);
const invalid = await request(app)
  .put('/api/workbooks/1/worksheets/s1/cells/0/0')
  .send({});
expect(invalid.status).toBe(400);
const root = await request(app).get('/');
expect(root.status).toBe(200);
"""

HACKATHON_WORKBOOKS_CARD_SPEC = (
    "Added POST /api/workbooks -> 201 { id, name } ; "
    "400 { error: 'WORKBOOK_NAME_REQUIRED' } ; 500 { error }. "
    "Existing GET /api/workbooks -> 200 { workbooks } ; "
    "GET /api/workbooks/:id -> 200 detail | 404 WORKBOOK_NOT_FOUND unchanged. "
    "PUT /api/workbooks/:id/worksheets/:worksheetId/cells/:rowIndex/:colIndex "
    "body { value } -> 200 { ok: true } | 404 { error: 'WORKBOOK_NOT_FOUND' } "
    "| 400 { error: 'INVALID_VALUE' } ; 500 { error }. Non-numeric ids -> 404."
)


def _hackathon_workbooks_interfaces() -> list[dict]:
    return [
        {
            "interface_id": "REQ-1-1-1-API-Workbooks",
            "type": "API",
            "name": "Workbooks REST router",
            "file_path": "backend/src/routes/workbooks.js",
            "first_line": "router.post('/', async (req, res) => {",
            "responsibility": "REST boundary extended with blank workbook creation.",
            "specification": HACKATHON_WORKBOOKS_CARD_SPEC,
        },
        {
            "interface_id": "REQ-1-1-1-FUNC-App",
            "type": "FUNC",
            "name": "Backend app entry",
            "file_path": "backend/src/app.js",
            "responsibility": (
                "Shared app entry serving the workbooks router and frontend dist on port 3301."
            ),
            "specification": (
                "app.use('/api/workbooks', workbooksRouter) before SPA fallback; "
                "static serving untouched."
            ),
        },
    ]


def test_validate_http_status_contract_accepts_pipe_status_card_replay(tmp_project_dir: Path) -> None:
    """Inline replay of the 2026-09-26 hackathon-sheet REQ-1-1-1 rejection.

    The real run rejected every assertion in this exact shape with seven
    diagnostics: the card's ``|``-separated statuses were dropped (detail
    404, PUT 400), the router-relative ``/:id`` skeleton record (404) was
    matched against the list assertion, and the SPA root assertion had no
    API contract. Every assertion matches the declared contract.
    """

    routes_path = tmp_project_dir / "backend" / "src" / "routes" / "workbooks.js"
    routes_path.parent.mkdir(parents=True)
    routes_path.write_text(HACKATHON_WORKBOOKS_SKELETON, encoding="utf-8")
    test_path = (
        tmp_project_dir
        / "backend"
        / "tests"
        / "generated"
        / "req_1_1_1_ff92cc72"
        / "integration"
        / "workbooksApi.test.js"
    )
    test_path.parent.mkdir(parents=True)
    test_path.write_text(HACKATHON_WORKBOOKS_INTEGRATION, encoding="utf-8")
    manifest = [
        {
            "test_id": "REQ-1-1-1-INT-WorkbooksApi",
            "req_id": "REQ-1-1-1",
            "type": "Integration",
            "coverage_scope": "owned",
            "interface_ids": ["REQ-1-1-1-API-Workbooks", "REQ-1-1-1-FUNC-App"],
            "file_path": str(test_path.relative_to(tmp_project_dir)).replace("\\", "/"),
        }
    ]

    assert validate_http_status_contracts(
        tmp_project_dir,
        {"description": "View and open a workbook; edit a cell value."},
        _hackathon_workbooks_interfaces(),
        manifest,
    ) == []


def test_validate_http_status_contract_root_assertion_without_non_api_owner_stays_needs_info(
    tmp_project_dir: Path,
) -> None:
    """The root-path skip is bound to a declared non-API owner.

    When the manifest names only API interfaces, a bare ``GET /`` assertion
    has no API contract and stays a needs-info diagnostic instead of being
    silently accepted.
    """

    test_path = tmp_project_dir / "tests" / "root.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const root = await request(app).get('/');\n"
        "expect(root.status).toBe(200);\n",
        encoding="utf-8",
    )
    manifest = [
        {
            "type": "Integration",
            "file_path": "tests/root.test.js",
            "interface_ids": ["REQ-1-1-1-API-Workbooks"],
        }
    ]

    diagnostics = validate_http_status_contracts(
        tmp_project_dir,
        {"description": "View and open a workbook."},
        [
            {
                "interface_id": "REQ-1-1-1-API-Workbooks",
                "type": "API",
                "specification": "GET /api/workbooks -> 200 { workbooks }.",
                "file_path": "backend/src/routes/workbooks.js",
            }
        ],
        manifest,
    )

    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "status_code_needs_info"


def test_validate_http_status_contract_static_declared_path_beats_relative_dynamic_record(
    tmp_project_dir: Path,
) -> None:
    """A statically declared route owns its assertions; sibling ``/:id`` records don't.

    The same misattribution class as the REQ-1-1-1 list-route conflict, one
    mount deeper: the dynamic record's swallowed tail is a sibling STATIC
    route (``/:id`` vs ``/api/auth/register``). The static routes here
    declare their statuses on the card only (placeholder skeleton bodies),
    so without the precedence rule the ``/:id`` record's 404 is the only
    matched route code and both assertions are rejected as conflicts.
    """

    routes_path = tmp_project_dir / "backend" / "src" / "routes" / "auth.js"
    routes_path.parent.mkdir(parents=True)
    routes_path.write_text(
        "const router = require('express').Router();\n"
        "router.get('/register', (req, res) => {\n"
        "  res.status(501).json({ code: 'NOT_IMPLEMENTED' });\n"
        "});\n"
        "router.get('/session', (req, res) => {\n"
        "  res.status(501).json({ code: 'NOT_IMPLEMENTED' });\n"
        "});\n"
        "router.get('/:id', (req, res) => {\n"
        "  if (!req.params.id) {\n"
        "    res.status(404).json({ error: 'NOT_FOUND' });\n"
        "    return;\n"
        "  }\n"
        "  res.status(501).json({ code: 'NOT_IMPLEMENTED' });\n"
        "});\n",
        encoding="utf-8",
    )
    test_path = tmp_project_dir / "tests" / "auth.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const register = await request(app).get('/api/auth/register');\n"
        "expect(register.status).toBe(201);\n"
        "const session = await request(app).get('/api/auth/session');\n"
        "expect(session.status).toBe(200);\n",
        encoding="utf-8",
    )
    card = {
        "interface_id": "REQ-1-API-Auth",
        "type": "API",
        "specification": (
            "GET /api/auth/register -> 201 { user }. "
            "GET /api/auth/session -> 200 { user }."
        ),
        "file_path": "backend/src/routes/auth.js",
    }
    manifest = [
        {
            "type": "Integration",
            "file_path": "tests/auth.test.js",
            "interface_ids": [card["interface_id"]],
        }
    ]

    assert validate_http_status_contracts(
        tmp_project_dir,
        {"description": "Register a traveler account."},
        [card],
        manifest,
    ) == []
