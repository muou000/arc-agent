from __future__ import annotations

import re
from pathlib import PureWindowsPath


_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


def normalize_safe_relative_path(value: str) -> str | None:
    """Normalize a generated path only when it is safe and relative.

    Test paths are model-generated input. Keep the accepted grammar deliberately
    small so the resulting path can never contain traversal segments or shell
    metacharacters before it reaches an app-type runner.
    """

    normalized = str(value or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized.startswith("/"):
        return None

    windows_path = PureWindowsPath(normalized)
    if windows_path.is_absolute() or windows_path.drive:
        return None

    segments = normalized.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        return None
    if any(_SAFE_PATH_SEGMENT.fullmatch(segment) is None for segment in segments):
        return None
    return normalized


def is_scoped_test_path(
    value: str,
    *,
    prefixes: tuple[str, ...],
    suffixes: tuple[str, ...],
) -> bool:
    normalized = normalize_safe_relative_path(value)
    if normalized is None:
        return False
    return normalized.startswith(prefixes) and normalized.endswith(suffixes)
