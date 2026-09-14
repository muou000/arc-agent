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


_DB_INIT_SEMANTICS_CHECK = """\
// Contract: initializeDatabase() always resolves to an open handle for the
// current database path, even when closeDb()/setDbPath() race an in-flight
// first init (regression for SQLITE_MISUSE: Database is closed).
// The close-race MUST be the first init in the process: later calls hit the
// memoized path and can self-heal, hiding the bug.
process.chdir(__dirname);
const assert = require('assert');
const { initializeDatabase, closeDb } = require('./src/database/init_db');

const unhandled = [];
process.on('unhandledRejection', (err) => unhandled.push(err));

async function usable(handle, label) {
  await new Promise((resolve, reject) => {
    handle.run('SELECT 1 AS one', (err) => {
      if (err) reject(new Error(`${label}: ${err.message}`));
      else resolve();
    });
  });
}

async function main() {
  const racing = initializeDatabase();
  await closeDb();
  await usable(await racing, 'close-race');

  const h1 = await initializeDatabase();
  const h2 = await initializeDatabase();
  assert.strictEqual(h2, h1, 'sequential second call should reuse the open handle');
  await usable(h2, 'second-call');

  const switched = await initializeDatabase({ dbPath: 'check-switch.sqlite' });
  await usable(switched, 'dbPath-switch');

  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.deepStrictEqual(unhandled, [], 'no unhandled rejections expected');

  await closeDb();
  require('fs').rmSync('database.db', { force: true });
  require('fs').rmSync('check-switch.sqlite', { force: true });
  console.log('DB_INIT_SEMANTICS_OK');
}

main()
  .then(() => process.exit(0))
  .catch((err) => {
    console.error(err && err.message ? err.message : err);
    process.exit(1);
  });
"""


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

    def test_database_init_handle_semantics(self, installed_backend: Path) -> None:
        """initializeDatabase() must never hand out a closed handle, even when
        closeDb()/setDbPath() race an in-flight first init."""
        script = installed_backend / "db_init_semantics_check.js"
        script.write_text(_DB_INIT_SEMANTICS_CHECK, encoding="utf-8")
        proc = subprocess.run(
            [_NODE_BIN, script.name],
            cwd=str(installed_backend),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout
        assert "DB_INIT_SEMANTICS_OK" in proc.stdout

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


def text_for(path: Path) -> str:
    return path.read_text(encoding="utf-8")