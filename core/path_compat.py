from __future__ import annotations

from pathlib import Path


def normalize_windows_extended_prefix_text(value: str | Path | None) -> str:
    r"""Strip Windows extended-length path prefixes from a path string.

    The agent filesystem backend can surface `\\?\`-prefixed paths on
    Windows when `Path.resolve()` is involved. Those paths refer to the same
    location as their normal counterparts, but string-based containment checks
    treat them as different roots. Normalizing them keeps path comparisons
    stable.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.replace("\\", "/")
    if normalized.startswith("//?/UNC/"):
        return "//" + normalized[len("//?/UNC/") :]
    if normalized.startswith("//?/"):
        return normalized[len("//?/") :]
    return normalized


def normalize_windows_extended_prefix_path(value: str | Path) -> Path:
    return Path(normalize_windows_extended_prefix_text(value))


def normalize_workspace_relative_path(value: object, workspace_path: str) -> str:
    """Normalize a manifest/agent path to a workspace-relative POSIX path.

    Accepts virtual ``/workspace/`` roots (the agent filesystem view),
    absolute host paths and plain relative paths, and strips ``./`` noise so
    manifest registration and run_tests requests compare equal strings.
    """

    path = normalize_windows_extended_prefix_text(value)
    if not path:
        return ""
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if path == "/workspace":
        return ""
    if path.startswith("/workspace/"):
        return path[len("/workspace/") :].lstrip("/")

    workspace = normalize_windows_extended_prefix_text(Path(workspace_path).expanduser().resolve()).rstrip("/")
    if path == workspace:
        return ""
    if path.startswith(workspace + "/"):
        return path[len(workspace) + 1 :].lstrip("/")
    return path.lstrip("/")
