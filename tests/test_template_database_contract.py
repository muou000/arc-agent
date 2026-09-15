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