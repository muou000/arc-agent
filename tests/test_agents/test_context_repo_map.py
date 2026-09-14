"""Regression tests for the live workspace map in ``<project_structure>``.

The 12306 full-run metrics (2026-09-13/14) showed exploration-class tools at
89.3% of all tool calls because every sibling node re-derived the same
template layout. The map turns that discovery into one deterministic scan:
file inventory with exported symbols, glue-anchor summaries for the shared
integration points, and per-file owning requirement IDs. These tests pin the
content, the owner annotations, the refresh semantics, the worktree-mode
map root, and the bounded rollup for mature workspaces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.context.pipeline import ContextPipeline
from agents.context.repo_map import MAX_INVENTORY_LINES


@pytest.fixture
def pipeline(arc_runtime) -> ContextPipeline:
    instance = ContextPipeline()
    instance.set_runtime(arc_runtime)
    return instance


def _write_map_workspace(workspace: Path) -> None:
    pages = workspace / "frontend" / "src" / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / "HomePage.tsx").write_text(
        "export default function HomePage() { return null; }\n", encoding="utf-8"
    )
    (pages / "LoginPage.tsx").write_text(
        "export default function LoginPage() { return null; }\n", encoding="utf-8"
    )
    api = workspace / "frontend" / "src" / "api"
    api.mkdir(parents=True, exist_ok=True)
    (api / "index.ts").write_text(
        "export async function apiGet(url: string) { return url; }\n"
        "export const API_BASE = '/api';\n",
        encoding="utf-8",
    )
    (workspace / "frontend" / "src" / "App.tsx").write_text(
        "import { Route, Routes } from 'react-router-dom';\n"
        "import HomePage from './pages/HomePage';\n"
        "import LoginPage from './pages/LoginPage';\n"
        "export default function App() {\n"
        "  return (\n"
        "    <Routes>\n"
        "      <Route path=\"/\" element={<HomePage />} />\n"
        "      <Route path=\"/login\" element={<LoginPage />} />\n"
        "    </Routes>\n"
        "  );\n"
        "}\n",
        encoding="utf-8",
    )
    backend = workspace / "backend" / "src"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "app.js").write_text(
        "const authRoutes = require('./routes/auth');\n"
        "app.use('/api/health');\n"
        "app.use('/api/auth', authRoutes);\n",
        encoding="utf-8",
    )
    database = backend / "database"
    database.mkdir(parents=True, exist_ok=True)
    (database / "init_db.js").write_text(
        "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )


def _configure(pipeline: ContextPipeline, workspace: Path) -> None:
    pipeline.configure(workspace_dir=str(workspace.resolve()), app_type="web")


def test_map_lists_source_files_with_exports(pipeline: ContextPipeline, tmp_project_dir: Path) -> None:
    _write_map_workspace(tmp_project_dir)
    _configure(pipeline, tmp_project_dir)

    structure = pipeline.get_static_context("REQ-1", "InterfaceDesigner")

    assert "- frontend/src/pages/HomePage.tsx exports: HomePage" in structure
    assert "- frontend/src/api/index.ts exports: apiGet, API_BASE" in structure


def test_map_includes_glue_anchor_summaries(pipeline: ContextPipeline, tmp_project_dir: Path) -> None:
    _write_map_workspace(tmp_project_dir)
    _configure(pipeline, tmp_project_dir)

    structure = pipeline.get_static_context("REQ-1", "InterfaceDesigner")

    assert "glue frontend/src/App.tsx (frontend route registration)" in structure
    assert "/ -> HomePage" in structure and "/login -> LoginPage" in structure
    assert "glue backend/src/app.js (backend route registration)" in structure
    assert "/api/auth" in structure and "require:./routes/auth" in structure
    assert "glue backend/src/database/init_db.js (database schema)" in structure
    assert "tables: users, orders" in structure


def test_anchor_extraction_tolerates_common_jsx_and_router_forms(pipeline: ContextPipeline, tmp_project_dir: Path) -> None:
    _write_map_workspace(tmp_project_dir)
    _configure(pipeline, tmp_project_dir)
    # Multiline element expressions and type-only imports are common in
    # agent-written JSX; router-mounted endpoints appear once route modules
    # register through a router instance.
    (tmp_project_dir / "frontend" / "src" / "App.tsx").write_text(
        "import { Route, Routes } from 'react-router-dom';\n"
        "import type RouteConfig from './route-config';\n"
        "import MultiPage from './pages/MultiPage';\n"
        "export default function App() {\n"
        "  return (\n"
        "    <Routes>\n"
        "      <Route\n"
        "        path=\"/multi\"\n"
        "        element={\n"
        "          <MultiPage />\n"
        "        }\n"
        "      />\n"
        "    </Routes>\n"
        "  );\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_project_dir / "backend" / "src" / "app.js").write_text(
        "const authRoutes = require('./routes/auth');\n"
        "const router = express.Router();\n"
        "router.post('/api/login');\n"
        "app.use('/api/auth', authRoutes);\n",
        encoding="utf-8",
    )

    structure = pipeline.get_static_context("REQ-2", "InterfaceDesigner")

    assert "/multi -> MultiPage" in structure, "multiline element expressions must still parse"
    assert "./pages/MultiPage" in structure, "value imports must be listed"
    assert "./route-config" not in structure, "type-only imports must be excluded"
    assert "/api/login" in structure, "router-mounted endpoints must be captured"


def test_map_annotates_interface_owners(pipeline: ContextPipeline, arc_runtime, tmp_project_dir: Path) -> None:
    _write_map_workspace(tmp_project_dir)
    _configure(pipeline, tmp_project_dir)
    store = arc_runtime.traceability
    store.upsert_interface(
        interface_id="IF-LOGIN",
        req_ids=["REQ-1.1"],
        type="ui_component",
        content="{}",
        file_path="/workspace/frontend/src/pages/LoginPage.tsx",
    )
    # A record whose path merely *ends like* the real file's path must not
    # be attributed to it (regression: bidirectional suffix matching).
    store.upsert_interface(
        interface_id="IF-SUFFIX",
        req_ids=["REQ-9.9"],
        type="ui_component",
        content="{}",
        file_path="pages/LoginPage.tsx",
    )

    structure = pipeline.get_static_context("REQ-2.2", "InterfaceDesigner")

    line = next(item for item in structure.splitlines() if item.startswith("- frontend/src/pages/LoginPage.tsx"))
    assert "[REQ-1.1]" in line, "the map must attribute files to the node that placed them"
    assert "REQ-9.9" not in structure, "suffix-colliding records must not leak into unrelated files"


def test_map_refreshes_after_file_layer_invalidation(pipeline: ContextPipeline, tmp_project_dir: Path) -> None:
    _write_map_workspace(tmp_project_dir)
    _configure(pipeline, tmp_project_dir)

    structure = pipeline.get_static_context("REQ-1", "InterfaceDesigner")
    assert "OrdersPage.tsx" not in structure

    (tmp_project_dir / "frontend" / "src" / "pages" / "OrdersPage.tsx").write_text(
        "export default function OrdersPage() { return null; }\n", encoding="utf-8"
    )
    pipeline.cache.invalidate_file_layers("REQ-1")

    refreshed = pipeline.get_static_context("REQ-1", "InterfaceDesigner")
    assert "OrdersPage.tsx" in refreshed, "a later sibling's file must be visible after invalidation"


def test_map_prefers_the_agent_workspace_root(pipeline: ContextPipeline, tmp_project_dir: Path, tmp_path: Path) -> None:
    _write_map_workspace(tmp_project_dir)
    agent_root = tmp_path / "task-worktree"
    pages = agent_root / "frontend" / "src" / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / "WorktreePage.tsx").write_text(
        "export default function WorktreePage() { return null; }\n", encoding="utf-8"
    )
    _configure(pipeline, tmp_project_dir)

    static = pipeline.get_static_context("REQ-1", "InterfaceDesigner", map_workspace_dir=str(agent_root))

    assert "WorktreePage.tsx" in static, "the map must describe the filesystem the agent explores"
    assert "frontend/src/api/index.ts" not in static, "main-workspace-only files must not leak into a worktree map"


def test_map_cache_isolates_per_map_root(pipeline: ContextPipeline, tmp_project_dir: Path, tmp_path: Path) -> None:
    # Regression: the layer key must carry the map root, otherwise the same
    # node built against a second worktree (e.g. --retry-failed in one
    # process) would hit the first root's cached map.
    _write_map_workspace(tmp_project_dir)
    _configure(pipeline, tmp_project_dir)
    other_root = tmp_path / "worktree-b"
    pages = other_root / "frontend" / "src" / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / "BPage.tsx").write_text(
        "export default function BPage() { return null; }\n", encoding="utf-8"
    )

    first = pipeline.get_static_context("REQ-1", "InterfaceDesigner")
    second = pipeline.get_static_context("REQ-1", "InterfaceDesigner", map_workspace_dir=str(other_root))
    third = pipeline.get_static_context("REQ-1", "InterfaceDesigner")

    assert "frontend/src/api/index.ts" in first
    assert "BPage.tsx" in second, "a different map root must not hit the first root's cache"
    assert "frontend/src/api/index.ts" not in second
    assert "frontend/src/api/index.ts" in third, "switching back must not hit the other root's cache"
    assert "BPage.tsx" not in third


def test_map_rolls_up_large_inventories(pipeline: ContextPipeline, arc_runtime, tmp_project_dir: Path) -> None:
    _write_map_workspace(tmp_project_dir)
    _configure(pipeline, tmp_project_dir)
    pages = tmp_project_dir / "frontend" / "src" / "pages"
    for index in range(MAX_INVENTORY_LINES + 20):
        (pages / f"F{index:04d}.tsx").write_text(
            f"export default function F{index:04d}() {{ return null; }}\n", encoding="utf-8"
        )
    store = arc_runtime.traceability
    store.upsert_interface(
        interface_id="IF-OWNED",
        req_ids=["REQ-1.1"],
        type="ui_component",
        content="{}",
        file_path="frontend/src/pages/HomePage.tsx",
    )

    static = pipeline.get_static_context("REQ-2.2", "InterfaceDesigner")

    assert "- frontend/src/pages/ (221 files)" in static, "anonymous files roll up per directory"
    assert "F0219" not in static, "anonymous per-file lines must be dropped once the cap is exceeded"
    owned = next(item for item in static.splitlines() if item.startswith("- frontend/src/pages/HomePage.tsx"))
    assert "[REQ-1.1]" in owned, "owner-annotated files keep per-file detail even in the rollup branch"


def test_map_without_a_workspace_falls_back_to_static_rules(pipeline: ContextPipeline, tmp_project_dir: Path) -> None:
    _configure(pipeline, tmp_project_dir)

    structure = pipeline.get_static_context("REQ-1", "InterfaceDesigner")

    assert "Web structure rules" in structure
    assert "Live workspace map" not in structure


def test_map_lives_in_the_static_split(pipeline: ContextPipeline, arc_runtime, tmp_project_dir: Path) -> None:
    _write_map_workspace(tmp_project_dir)
    _configure(pipeline, tmp_project_dir)
    store = arc_runtime.traceability
    store.upsert_requirement(req_id="REQ-1", name="Root")

    static, dynamic = pipeline.build_agent_context_split(node_id="REQ-1", agent_type="InterfaceDesigner")

    assert "Live workspace map" in static
    assert "Live workspace map" not in dynamic, "the map is static context, not per-node dynamic context"
