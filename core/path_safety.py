from __future__ import annotations

import os
from pathlib import Path


def validate_clean_target(
    output_dir: str | os.PathLike[str],
    requirement_dir: str | os.PathLike[str],
    *,
    repo_root: str | os.PathLike[str] | None = None,
) -> str | None:
    """Return an error when a destructive cleanup target is protected."""

    output = Path(output_dir).expanduser().resolve()
    requirements = Path(requirement_dir).expanduser().resolve()
    if output == Path(output.anchor):
        return "refusing to clean the filesystem root"
    if repo_root is not None and output == Path(repo_root).expanduser().resolve():
        return "refusing to clean the repository root"
    if output == Path.cwd().resolve():
        return "refusing to clean the current directory"

    if output == requirements or output in requirements.parents or requirements in output.parents:
        return "refusing to clean a path that overlaps the requirements directory"
    if ".git" in {part.lower() for part in output.parts}:
        return "refusing to clean a path inside .git"
    return None
