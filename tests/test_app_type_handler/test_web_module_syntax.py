"""Pin: app_type_handler/web.py compiles clean and its shell docstring survives.

Invalid ``\\``-style escapes in a regular docstring surface as a visible
SyntaxWarning on every import (Python 3.12+) and are slated to become
SyntaxError, which would hard-fail the import. The docstring must stay a raw
string whose rendered content keeps the literal backslash.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import app_type_handler.web


def test_web_module_compiles_without_invalid_escape_warning() -> None:
    source_path = Path(app_type_handler.web.__file__)
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        compile(source_path.read_text(encoding="utf-8"), str(source_path), "exec")


def test_shell_single_arg_docstring_keeps_literal_backslash_backtick() -> None:
    doc = app_type_handler.web._shell_single_arg.__doc__ or ""
    assert r"``\``" in doc
