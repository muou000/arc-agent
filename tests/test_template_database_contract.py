"""Fast static checks for the database bootstrap a workspace is generated with.

The bootstrap the compiler ships is the provisioned template plus the directed
patches in ``app_type_handler/template_patches``: the template itself is
provided by the platform, so a fix cannot live as a repo template edit and has
to be applied to the workspace right after the template is copied. These checks
run the bundled patches over the in-repo template and assert the result carries
none of the known defects.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app_type_handler.template_patches import (
    ALREADY_APPLIED,
    APPLIED,
    apply_template_patches,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPO_ROOT / "arc-template" / "templates" / "web-react-express"


@pytest.fixture(scope="module")
def patched_bootstrap(tmp_path_factory: pytest.TempPathFactory) -> str:
    """``init_db.js`` exactly as a generated workspace receives it."""

    workspace = tmp_path_factory.mktemp("patched-template-") / "workspace"
    shutil.copytree(TEMPLATE_ROOT, workspace)
    outcomes = apply_template_patches(str(workspace), "web-react-express")
    assert outcomes, "the web template must ship at least one patch"
    assert all(outcome.status in {APPLIED, ALREADY_APPLIED} for outcome in outcomes), outcomes
    return (workspace / "backend" / "src" / "database" / "init_db.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def patched_harness(tmp_path_factory: pytest.TempPathFactory) -> str:
    """``test_harness.js`` exactly as a generated workspace receives it."""

    workspace = tmp_path_factory.mktemp("patched-harness-") / "workspace"
    shutil.copytree(TEMPLATE_ROOT, workspace)
    outcomes = apply_template_patches(str(workspace), "web-react-express")
    assert all(outcome.status in {APPLIED, ALREADY_APPLIED} for outcome in outcomes), outcomes
    return (workspace / "backend" / "src" / "database" / "test_harness.js").read_text(
        encoding="utf-8"
    )


def test_init_db_never_returns_a_bare_init_promise(patched_bootstrap: str) -> None:
    """Returning the init promise hands callers a Promise<void>, not a handle."""

    assert "return initPromise;" not in patched_bootstrap


def test_init_promise_resolves_to_the_database_handle(patched_bootstrap: str) -> None:
    """Awaiting the memoized init must yield the database handle itself."""

    assert "return database;" in patched_bootstrap


def test_init_validates_generation_before_returning_the_handle(patched_bootstrap: str) -> None:
    """A concurrent closeDb()/setDbPath() must never yield a closed handle.

    initializeDatabase() re-checks that the handle it is about to return is
    still the current one and retries otherwise; otherwise the caller's next
    DB operation fails with "SQLITE_MISUSE: Database is closed".
    """

    assert "db === database" in patched_bootstrap


def test_close_db_absorbs_orphaned_init_rejections(patched_bootstrap: str) -> None:
    """An init interrupted by closeDb() must not become an unhandled rejection."""

    assert ".catch(() => {})" in patched_bootstrap


def test_genuine_init_failures_surface_instead_of_burning_retries(patched_bootstrap: str) -> None:
    """A rejection seen while its init promise is still current is genuine.

    Only an invalidation (closeDb/setDbPath/newer init) replaces or nulls
    ``initPromise``; anything else must surface immediately instead of
    consuming the bounded retry loop.
    """

    assert "if (initPromise === promise) {" in patched_bootstrap


def test_harness_opens_sqlite_only_inside_a_per_harness_scope_dir(patched_harness: str) -> None:
    """A concurrent cleanup must not be able to remove the directory a queued
    sqlite open is still materializing its file in.

    Harness cleanup removes its root directory when it is empty, and every
    harness used to write into that one shared ``.arc-test-db`` root - so one
    worker's cleanup could delete the directory between another worker's mkdir
    and its queued (asynchronous) sqlite open, failing the open with
    SQLITE_CANTOPEN {errno: 14}. Seen 2026-09-21 as a vitest unhandled error in
    a Unit run that cost three flaky retry rounds to self-heal. Nesting each
    harness's database in its own scope directory makes every removal
    self-owned: nobody deletes a directory another worker is still using.
    """

    assert "const scopeDir = path.join(rootDir," in patched_harness
    assert "rootDir: scopeDir" in patched_harness


def test_harness_reset_recreates_the_temp_dir_before_reopening(patched_harness: str) -> None:
    """reset() drives a fresh initializeDatabase() long after setup() ran.

    setup() mkdirs the root before opening sqlite; reset() reopens without
    that guarantee of its own. Re-creating the directory before the reopen is
    the harness-side mkdir -p guard: the reopen must never assume the directory
    setup() made still exists.
    """

    assert patched_harness.count("fs.mkdirSync(rootDir, { recursive: true });") == 2
    assert "Re-create the root directory before reopening sqlite" in patched_harness
