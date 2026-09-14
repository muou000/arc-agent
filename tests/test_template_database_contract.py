"""Fast static checks for the shipped template database bootstrap."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DATABASE = (
    REPO_ROOT
    / "arc-template"
    / "templates"
    / "web-react-express"
    / "backend"
    / "src"
    / "database"
)


def _source() -> str:
    return (TEMPLATE_DATABASE / "init_db.js").read_text(encoding="utf-8")


def test_init_db_never_returns_a_bare_init_promise() -> None:
    """Returning the init promise hands callers a Promise<void>, not a handle."""

    assert "return initPromise;" not in _source()


def test_init_promise_resolves_to_the_database_handle() -> None:
    """Awaiting the memoized init must yield the database handle itself."""

    assert "return database;" in _source()


def test_init_validates_generation_before_returning_the_handle() -> None:
    """A concurrent closeDb()/setDbPath() must never yield a closed handle.

    initializeDatabase() re-checks that the handle it is about to return is
    still the current one and retries otherwise; otherwise the caller's next
    DB operation fails with "SQLITE_MISUSE: Database is closed".
    """

    assert "db === database" in _source()


def test_close_db_absorbs_orphaned_init_rejections() -> None:
    """An init interrupted by closeDb() must not become an unhandled rejection."""

    assert ".catch(() => {})" in _source()
