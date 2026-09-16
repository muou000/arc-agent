"""Mechanical contract-skeleton extraction from materialized design files.

Pins the P0 repair channel for the 2026-09-16 flash-class failure shape:
models that write the design files but hand back a schema-valid empty
``interfaces`` array. The contract identities must be derivable from the
files themselves — every skeleton anchored to real code, shared-surface
edits marked as updates of already registered contracts, and nothing
invented.
"""

from __future__ import annotations

from pathlib import Path

from agents.design.contract_skeleton import (
    derive_contract_skeletons,
    merge_filled_contracts,
)


def _write(root: Path, rel_path: str, content: str) -> None:
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


ROUTER_FILE = """const express = require('express');
const authService = require('../services/auth_service');

/**
 * REQ-2 authentication HTTP boundary.
 */
const router = express.Router();

router.post('/login', async (req, res) => {
  res.json({});
});
router.get('/me', async (req, res) => {
  res.json({});
});

module.exports = router;
"""

SERVICE_FILE = """const crypto = require('crypto');

// REQ-2 authentication domain logic.
async function verifyCredentials(identifier, password) {
  return null;
}

module.exports = {
  verifyCredentials,
  createSessionForUser,
};
"""

INIT_DB_FILE = """const fs = require('fs');

// Use CREATE TABLE IF NOT EXISTS to create new tables.
async function runStatement(db, sql) {
  return db.run(sql);
}

const users = `
      CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY
      )
    `;
const sessions = `
      CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY
      )
    `;
"""

PAGE_FILE = """import { useState } from 'react';

/**
 * REQ-2 login page.
 */
export default function LoginPage() {
  const [identifier, setIdentifier] = useState('');
  return <form>{identifier}</form>;
}
"""

API_CLIENT_FILE = """import apiClient from '../../api';

export async function loginRequest(identifier, password) {
  const { data } = await apiClient.post('/auth/login', { identifier, password });
  return data.user;
}
"""


def _skeletons(root: Path, paths: list[str], node_id: str = "REQ-2"):
    return derive_contract_skeletons(
        node_id=node_id,
        file_paths=paths,
        workspace_root=str(root),
    )


def test_express_router_yields_api_skeleton(tmp_path: Path) -> None:
    _write(tmp_path, "backend/src/routes/auth_routes.js", ROUTER_FILE)

    skeletons = _skeletons(tmp_path, ["/workspace/backend/src/routes/auth_routes.js"])

    assert len(skeletons) == 1
    skeleton = skeletons[0]
    assert skeleton.type == "API"
    assert skeleton.interface_id == "REQ-2-API-AuthRoutes"
    assert skeleton.file_path == "backend/src/routes/auth_routes.js"
    assert skeleton.first_line == "const express = require('express');"
    # The first route of the file is surfaced for prompt context.
    assert skeleton.method == "post"
    assert skeleton.mount_path == "/login"
    assert skeleton.relation == "owned"


def test_service_module_yields_func_skeleton(tmp_path: Path) -> None:
    _write(tmp_path, "backend/src/services/auth_service.js", SERVICE_FILE)

    skeletons = _skeletons(tmp_path, ["/workspace/backend/src/services/auth_service.js"])

    assert len(skeletons) == 1
    skeleton = skeletons[0]
    assert skeleton.type == "FUNC"
    # The name comes from the module's real exports, not the doc comment.
    assert skeleton.name == "AuthService"
    assert skeleton.interface_id == "REQ-2-FUNC-AuthService"


def test_create_table_statements_yield_db_skeletons(tmp_path: Path) -> None:
    _write(tmp_path, "backend/src/database/init_db.js", INIT_DB_FILE)

    skeletons = _skeletons(tmp_path, ["/workspace/backend/src/database/init_db.js"])

    assert [s.interface_id for s in skeletons] == [
        "REQ-2-DB-UsersTable",
        "REQ-2-DB-SessionsTable",
    ]
    assert all(s.type == "DB" for s in skeletons)
    # CREATE TABLE mentioned only in a comment must not become a table.
    assert all(s.table_name in {"users", "sessions"} for s in skeletons)


def test_page_component_and_api_client_yield_ui_and_func(tmp_path: Path) -> None:
    _write(tmp_path, "frontend/src/pages/LoginPage.tsx", PAGE_FILE)
    _write(tmp_path, "frontend/src/features/auth/authApi.ts", API_CLIENT_FILE)

    skeletons = _skeletons(
        tmp_path,
        ["/workspace/frontend/src/pages/LoginPage.tsx", "/workspace/frontend/src/features/auth/authApi.ts"],
    )

    by_id = {s.interface_id: s for s in skeletons}
    assert by_id["REQ-2-UI-LoginPage"].type == "UI"
    assert by_id["REQ-2-UI-LoginPage"].name == "LoginPage"
    # The api client under frontend/.../api is a FUNC boundary, not a UI one.
    assert by_id["REQ-2-FUNC-AuthApi"].type == "FUNC"


def test_shared_file_with_registered_contract_becomes_update(tmp_path: Path) -> None:
    _write(tmp_path, "backend/src/app.js", "const express = require('express');\nconst app = express();\n")

    skeletons = derive_contract_skeletons(
        node_id="REQ-2",
        file_paths=["/workspace/backend/src/app.js"],
        workspace_root=str(tmp_path),
        interface_ids_by_file={"backend/src/app.js": {"ROOT-API-ExpressApp"}},
    )

    assert len(skeletons) == 1
    assert skeletons[0].relation == "update"
    assert skeletons[0].interface_id == "ROOT-API-ExpressApp"
    # Update rows carry no new type: the existing contract keeps its identity.
    assert skeletons[0].type == ""


def test_missing_and_outside_files_yield_nothing(tmp_path: Path) -> None:
    _write(tmp_path, "backend/src/routes/real.js", ROUTER_FILE)

    skeletons = _skeletons(
        tmp_path,
        [
            "/workspace/backend/src/routes/missing.js",
            "/workspace/../outside.js",
            "",
        ],
    )

    assert skeletons == []


def test_unrecognized_extension_yields_nothing(tmp_path: Path) -> None:
    _write(tmp_path, "docs/readme.md", "# notes\n")

    assert _skeletons(tmp_path, ["/workspace/docs/readme.md"]) == []


def test_merge_keeps_skeleton_identity_and_model_semantics(tmp_path: Path) -> None:
    _write(tmp_path, "backend/src/routes/auth_routes.js", ROUTER_FILE)
    skeletons = _skeletons(tmp_path, ["/workspace/backend/src/routes/auth_routes.js"])

    # The model answers with a semantic-only row keyed by interface_id...
    records = [
        {
            "interface_id": "REQ-2-API-AuthRoutes",
            "responsibility": "Auth HTTP boundary.",
            "specification": "POST /api/auth/login, GET /api/auth/me.",
        },
        # ...plus a reused parent interface not in the skeleton list.
        {
            "interface_id": "ROOT-UI-AppHeader",
            "req_id": "ROOT",
            "type": "UI",
            "name": "AppHeader",
            "file_path": "frontend/src/components/AppHeader.tsx",
            "responsibility": "Reused parent header.",
        },
    ]

    merged = merge_filled_contracts(skeletons, records)

    assert len(merged) == 2
    api = merged[0]
    assert api["interface_id"] == "REQ-2-API-AuthRoutes"
    assert api["type"] == "API"
    assert api["file_path"] == "backend/src/routes/auth_routes.js"
    assert api["responsibility"] == "Auth HTTP boundary."
    assert api["first_line"] == "const express = require('express');"
    reused = merged[1]
    assert reused["interface_id"] == "ROOT-UI-AppHeader"
    assert reused["responsibility"] == "Reused parent header."
    # No fabricated path may enter through the skeleton channel.
    assert all("file_path" in record for record in merged)


def test_merge_matches_model_rows_by_file_path_when_ids_differ(tmp_path: Path) -> None:
    _write(tmp_path, "backend/src/services/auth_service.js", SERVICE_FILE)
    skeletons = _skeletons(tmp_path, ["/workspace/backend/src/services/auth_service.js"])

    # The model minted its own id for the row but pointed at the right file.
    records = [
        {
            "interface_id": "IF-MODEL-OWN",
            "file_path": "backend/src/services/auth_service.js",
            "responsibility": "Session verification logic.",
            "specification": "verifyCredentials returns null on any failure.",
        }
    ]

    merged = merge_filled_contracts(skeletons, records)

    assert len(merged) == 1
    # The mechanical identity wins over the model-minted id.
    assert merged[0]["interface_id"] == "REQ-2-FUNC-AuthService"
    assert merged[0]["responsibility"] == "Session verification logic."
