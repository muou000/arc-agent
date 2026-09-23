"""Pin: the web handler modules compile clean and the shell docstring survives.

Invalid ``\\``-style escapes in a regular docstring surface as a visible
SyntaxWarning on every import (Python 3.12+) and are slated to become
SyntaxError, which would hard-fail the import. The docstring must stay a raw
string whose rendered content keeps the literal backslash. The compile pin
covers both modules of the E2E execution path: the app-type handler and the
attempt pipeline that owns the ``_shell_single_arg`` quoting helper.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import app_type_handler.e2e_attempt
import app_type_handler.web


def test_web_module_compiles_without_invalid_escape_warning() -> None:
    for module in (app_type_handler.web, app_type_handler.e2e_attempt):
        source_path = Path(module.__file__)
        with warnings.catch_warnings():
            warnings.simplefilter("error", SyntaxWarning)
            compile(source_path.read_text(encoding="utf-8"), str(source_path), "exec")


def test_shell_single_arg_docstring_keeps_literal_backslash_backtick() -> None:
    doc = app_type_handler.e2e_attempt._shell_single_arg.__doc__ or ""
    assert r"``\``" in doc
