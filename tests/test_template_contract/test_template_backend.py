"""Slow integration tests for the backend portion of ``template/``.

These tests require a working Node.js + npm installation. They are skipped by
default; run them with::

    pytest -m slow tests/test_template_contract

A scratch copy of the template is created so the tests never mutate the
checked-in template.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_BACKEND = (
    REPO_ROOT / "arc-template" / "templates" / "web-react-express" / "backend"
)


def _resolve(binary: str) -> str:
    """Resolve a binary to an absolute path, preferring Windows-known names."""

    for name in (binary, binary + ".exe", binary + ".cmd", binary + ".ps1"):
        found = shutil.which(name)
        if found:
            return found
    return binary


def _has_node() -> bool:
    # On Windows, ``shutil.which`` returns ``None`` for ``node.exe`` even when
    # the binary is on PATH, because Node's installer registers a cmd shim.
    # Probe both ``node`` and the explicit ``node.exe`` form to be safe.
    candidates = ["node", "node.exe", "npm", "npm.cmd", "npm.ps1"]
    return any(shutil.which(c) for c in candidates)


_NODE_BIN = _resolve("node") if _has_node() else "node"
_NPM_BIN = _resolve("npm") if _has_node() else "npm"


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not _has_node(), reason="node/npm not available on PATH"),
]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_health(port: int, *, timeout: float = 15.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=1
            ) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


@pytest.fixture(scope="module")
def installed_backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Copy the backend template, run ``npm install``, and yield the directory."""

    scratch = tmp_path_factory.mktemp("template-backend-")
    shutil.copytree(TEMPLATE_BACKEND, scratch / "backend")
    proc = subprocess.run(
        [_NPM_BIN, "install", "--no-audit", "--no-fund", "--prefer-offline"],
        cwd=str(scratch / "backend"),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        pytest.skip(f"npm install failed: {proc.stderr or proc.stdout}")
    yield scratch / "backend"


class TestBackendContract:
    def test_app_module_loads(self, installed_backend: Path) -> None:
        proc = subprocess.run(
            [_NODE_BIN, "-e", "require('./src/app.js'); console.log('ok')"],
            cwd=str(installed_backend),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0, proc.stderr
        assert "ok" in proc.stdout

    def test_health_endpoint(self, installed_backend: Path) -> None:
        port = _free_port()
        env = {"PORT": str(port)}
        # Start the server detached and wait for /api/health.
        proc = subprocess.Popen(
            [_NODE_BIN, "src/index.js"],
            cwd=str(installed_backend),
            env={**os.environ, **env},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert _wait_for_health(port), "backend never responded to /api/health"
            import urllib.request

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=2
            ) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            assert payload == {"code": 200, "message": "Backend Ready"}
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_vitest_config_targets_tests_dir(self) -> None:
        text = (TEMPLATE_BACKEND / "vitest.config.js").read_text(encoding="utf-8")
        assert "tests/" in text

    def test_package_json_test_scripts(self) -> None:
        pkg = json.loads((TEMPLATE_BACKEND / "package.json").read_text(encoding="utf-8"))
        scripts = pkg["scripts"]
        assert "test" in scripts
        assert "test:e2e" in scripts
        assert "test:all" in scripts
        # e2e test dir exists
        assert "test-e2e" in text_for(TEMPLATE_BACKEND / "playwright.config.js")


class TestRouteRegistry:
    """Registration-based glue: route and schema modules contributed after
    install must be mounted/loaded without touching app.js or init_db.js.

    These tests write modules into the shared ``installed_backend`` scratch, so
    they must run after the pristine-server tests above (class definition
    order).
    """

    _SCHEMA_MODULE = """'use strict';

module.exports = {
  order: 10,
  async apply(db) {
    await new Promise((resolve, reject) => {
      db.run(
        'CREATE TABLE IF NOT EXISTS registry_probe (id INTEGER PRIMARY KEY, name TEXT)',
        (err) => (err ? reject(err) : resolve()),
      );
    });
  },
};
"""

    _ROUTE_MODULE = """'use strict';

const express = require('express');
const { getDb, initializeDatabase } = require('../database/init_db');

const router = express.Router();

router.get('/probe', async (req, res) => {
  try {
    await initializeDatabase();
    getDb().all(
      "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'registry_probe'",
      [],
      (err, rows) => {
        if (err) {
          res.status(500).json({ error: String(err) });
          return;
        }
        res.json({ tableRegistered: rows.length > 0 });
      },
    );
  } catch (error) {
    res.status(500).json({ error: String(error) });
  }
});

module.exports = { mountPath: '/api/registry-probe', router };
"""

    def _write_modules(self, installed_backend: Path) -> None:
        routes_dir = installed_backend / "src" / "routes"
        routes_dir.mkdir(exist_ok=True)
        (routes_dir / "registry_probe.routes.js").write_text(self._ROUTE_MODULE, encoding="utf-8")
        schema_dir = installed_backend / "src" / "database" / "schema"
        schema_dir.mkdir(exist_ok=True)
        (schema_dir / "registry_probe.schema.js").write_text(self._SCHEMA_MODULE, encoding="utf-8")

    def test_route_and_schema_modules_mount_and_load(self, installed_backend: Path) -> None:
        self._write_modules(installed_backend)

        port = _free_port()
        proc = subprocess.Popen(
            [_NODE_BIN, "src/index.js"],
            cwd=str(installed_backend),
            env={**os.environ, "PORT": str(port)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert _wait_for_health(port), "backend never responded to /api/health"
            import urllib.request

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/registry-probe/probe", timeout=5
            ) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            assert payload == {"tableRegistered": True}, (
                "route module was not mounted or schema module was not applied"
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_register_routes_reports_mounted_paths(self, installed_backend: Path) -> None:
        script = (
            "const { registerRoutes } = require('./src/routes');"
            "registerRoutes({ use: (p, r) => process.stdout.write(JSON.stringify(p)) });"
        )
        proc = subprocess.run(
            [_NODE_BIN, "-e", script],
            cwd=str(installed_backend),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == '"/api/registry-probe"'


def text_for(path: Path) -> str:
    return path.read_text(encoding="utf-8")