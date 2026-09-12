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


def test_init_db_returns_a_handle_when_memoized() -> None:
    """The memoized branch must resolve to the database, not to the promise."""

    source = (TEMPLATE_DATABASE / "init_db.js").read_text(encoding="utf-8")

    assert "return initPromise;" not in source
    assert "await initPromise;" in source
    assert "return getDb();" in source
