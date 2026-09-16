"""REQ-2 replay: the 2026-09-16 run6 failure shape recovered end to end.

deepseek-v4-flash materialized 12 design files for REQ-2 (login) and returned
a schema-valid empty ``interfaces`` array on both the main pass and the re-ask;
the node died at the workflow's empty-interface hard gate. This replay pins
the repaired behaviour on the real run6 file shapes: owned-file writes, one
batched edit round on shared surfaces, registered parent contracts on the
shared files, and a flash-realistic fill answer that covers every derived
skeleton row.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agents.interface_designer import InterfaceDesigner
from tests.helpers.faux import FauxChatModel, faux_tool_call, faux_tool_calls

ROUTER = """const express = require('express');
const authService = require('../services/auth_service');

// REQ-2 authentication HTTP boundary.
const router = express.Router();

router.post('/login', async (req, res) => {
  res.json({});
});
router.get('/me', async (req, res) => {
  res.json({});
});

module.exports = router;
"""

SERVICE = """const crypto = require('crypto');

// REQ-2 authentication domain logic.
async function verifyCredentials(identifier, password) {
  return null;
}

module.exports = {
  verifyCredentials,
};
"""

REPOSITORY = """const { get, run } = require('../database/db_runtime');

// REQ-2 persistence boundary for authentication.
async function findUserByUsername(username) {
  return get('SELECT * FROM users WHERE username = ?', [username]);
}

module.exports = {
  findUserByUsername,
};
"""

INIT_DB = """const fs = require('fs');

// Guide: Use CREATE TABLE IF NOT EXISTS to create new tables.
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

SEED_DB = """const { closeDb } = require('./init_db');

// REQ-2 demo account seeding.
async function seedDatabase() {
  return null;
}

module.exports = seedDatabase;
"""

APP_JS = """const express = require('express');
const app = express();

// register routes
app.get('/api/health', (req, res) => {
  res.json({ status: 'ok' });
});

module.exports = app;
"""

APP_TSX = """import { Route, Routes } from 'react-router-dom';
import HomePage from './pages/HomePage';

function App() {
  return (
    <Routes>
      <Route path="/" element={<HomePage />} />
    </Routes>
  );
}

export default App;
"""

APP_HEADER = """import { Link } from 'react-router-dom';

// ROOT-owned app shell header with attachment slots.
function AppHeader() {
  return <header data-testid="app-header" />;
}

export default AppHeader;
"""

AUTH_CONTEXT = """import { createContext, useCallback, useContext, useState } from 'react';

/**
 * REQ-2 global session state.
 */
export function AuthProvider({ children }) {
  return children;
}
"""

AUTH_CONTROLS = """import { Link } from 'react-router-dom';
import { useAuth } from './AuthContext';

// REQ-2 auth controls for the ROOT AppHeader slot.
function AuthControls() {
  return <span data-testid="auth-controls" />;
}

export default AuthControls;
"""

AUTH_API = """import apiClient from '../../api';

// REQ-2 auth API client.
export async function loginRequest(identifier, password) {
  const { data } = await apiClient.post('/auth/login', { identifier, password });
  return data.user;
}
"""

LOGIN_PAGE = """import { useState } from 'react';

/**
 * REQ-2 login page (/login).
 */
export default function LoginPage() {
  const [identifier, setIdentifier] = useState('');
  return <form>{identifier}</form>;
}
"""

OWNED_FILES = {
    "backend/src/routes/auth_routes.js": ROUTER,
    "backend/src/services/auth_service.js": SERVICE,
    "backend/src/repositories/auth_repository.js": REPOSITORY,
    "frontend/src/features/auth/AuthContext.tsx": AUTH_CONTEXT,
    "frontend/src/features/auth/AuthControls.tsx": AUTH_CONTROLS,
    "frontend/src/features/auth/authApi.ts": AUTH_API,
    "frontend/src/pages/LoginPage.tsx": LOGIN_PAGE,
}

SHARED_FILES = {
    "backend/src/database/init_db.js": INIT_DB,
    "backend/src/database/seed_db.js": SEED_DB,
    "backend/src/app.js": APP_JS,
    "frontend/src/App.tsx": APP_TSX,
    "frontend/src/components/layout/AppHeader.tsx": APP_HEADER,
}

REGISTERED_PARENT_CONTRACTS = [
    ("ROOT-API-ExpressApp", "backend/src/app.js", "API"),
    ("ROOT-UI-AppShell", "frontend/src/App.tsx", "UI"),
    ("ROOT-UI-AppHeader", "frontend/src/components/layout/AppHeader.tsx", "UI"),
]

FILL_ROWS = [
    {"interface_id": "REQ-2-API-AuthRoutes", "responsibility": "Auth HTTP boundary.", "specification": "login/me/logout with session cookie."},
    {"interface_id": "REQ-2-FUNC-AuthService", "responsibility": "Credential verification.", "specification": "generic failure on bad credentials."},
    {"interface_id": "REQ-2-FUNC-AuthRepository", "responsibility": "Persistence boundary.", "specification": "users/sessions CRUD."},
    {"interface_id": "REQ-2-FUNC-AuthApi", "responsibility": "Auth API client.", "specification": "loginRequest/getCurrentUser."},
    {"interface_id": "REQ-2-UI-LoginPage", "responsibility": "Login page at /login.", "specification": "native-label controls."},
    {"interface_id": "REQ-2-UI-AuthProvider", "responsibility": "Global session state.", "specification": "useAuth provider."},
    {"interface_id": "REQ-2-UI-AuthControls", "responsibility": "Header auth controls.", "specification": "anonymous/authed states."},
    {"interface_id": "REQ-2-DB-UsersTable", "responsibility": "Users schema.", "specification": "username unique."},
    {"interface_id": "REQ-2-DB-SessionsTable", "responsibility": "Sessions schema.", "specification": "token PK."},
    {"interface_id": "REQ-2-FUNC-SeedDb", "responsibility": "Seed bootstrap.", "specification": "demo account."},
    {"interface_id": "ROOT-UI-AppShell", "type": "UI", "responsibility": "Extended with auth route.", "specification": "additive edit."},
    {"interface_id": "ROOT-API-ExpressApp", "type": "API", "responsibility": "Extended with /api/auth.", "specification": "additive edit."},
    {"interface_id": "ROOT-UI-AppHeader", "type": "UI", "responsibility": "Extended with AuthControls.", "specification": "additive edit."},
]


def test_req2_run6_failure_shape_recovers_all_contracts(
    tmp_project_dir: Path, arc_runtime
) -> None:
    node_id = "REQ-2"
    arc_runtime.traceability.store_requirement_tree(
        {"id": node_id, "name": "登录", "description": "账号密码登录"}
    )
    # Shared surfaces already carry the parent's registered contracts, exactly
    # like the integration HEAD REQ-2 started from in run6.
    for interface_id, path, interface_type in REGISTERED_PARENT_CONTRACTS:
        arc_runtime.traceability.upsert_interface(
            interface_id=interface_id,
            req_ids=["ROOT"],
            type=interface_type,
            content="{}",
            file_path=path,
        )
    for rel_path, content in {**OWNED_FILES, **SHARED_FILES}.items():
        target = tmp_project_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    writes = [
        faux_tool_call(
            "write_file",
            {"file_path": f"/workspace/{rel_path}", "content": content},
            call_id=f"w{index}",
        )
        for index, (rel_path, content) in enumerate(OWNED_FILES.items())
    ]
    # run6 shape: one assistant turn batching every shared-surface edit.
    batch = faux_tool_calls(
        *(
            (
                "edit_file",
                {
                    "file_path": f"/workspace/{rel_path}",
                    "old_string": content.split("\n")[0],
                    "new_string": content.split("\n")[0] + " // REQ-2",
                },
                f"e{index}",
            )
            for index, (rel_path, content) in enumerate(SHARED_FILES.items())
        )
    )
    model = FauxChatModel(
        responses=[
            *writes,
            batch,
            # Main pass: files materialized, schema-valid empty interfaces.
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": "REQ-2 login chain designed.", "interfaces": [], "files_written": []},
                call_id="final-empty",
            ),
            # Skeleton-guided fill: every derived row answered.
            faux_tool_call(
                "InterfaceDesignRepairResponse",
                {"summary": "Filled all rows.", "interfaces": FILL_ROWS, "files_written": []},
                call_id="fill",
            ),
        ]
    )
    designer = InterfaceDesigner(
        model=model,
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
    )

    bundle = asyncio.run(
        designer.run(node_id=node_id, requirement_data={"name": "登录", "description": "账号密码登录"})
    )

    by_id = {item["interface_id"]: item for item in bundle["interfaces"]}
    # All 12 materialized files are represented: 10 owned contracts plus the
    # two tables in init_db.js, and the three shared files are update rows of
    # the parent's registered contracts.
    assert len(bundle["interfaces"]) == 13
    assert not any(item.get("skeleton_derived") for item in bundle["interfaces"])
    # DB layer: both CREATE TABLE statements became contracts.
    assert by_id["REQ-2-DB-UsersTable"]["type"] == "DB"
    assert by_id["REQ-2-DB-SessionsTable"]["type"] == "DB"
    # Shared-surface edits are updates of the parent contracts, not new rows.
    for interface_id, _, interface_type in REGISTERED_PARENT_CONTRACTS:
        assert by_id[interface_id]["relation"] == "update"
        assert by_id[interface_id]["type"] == interface_type
        assert by_id[interface_id]["responsibility"]
    # Model semantics survive the mechanical merge.
    assert by_id["REQ-2-API-AuthRoutes"]["responsibility"] == "Auth HTTP boundary."
    assert by_id["REQ-2-FUNC-AuthService"]["file_path"] == "backend/src/services/auth_service.js"
    # The workflow hard gate sees the discipline's ground truth.
    assert len(bundle["materialized_paths"]) == 12
