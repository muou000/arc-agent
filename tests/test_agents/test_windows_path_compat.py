"""Windows extended-length path forms must not break the agent filesystem.

On Windows, ``Path.resolve()`` can surface ``\\\\?\\``-prefixed paths for
some or all of a workspace root and its children (long paths on machines
without ``LongPathsEnabled``, junctions, ``GetFinalPathNameByHandle``
outputs). Upstream's virtual containment check compares those raw string
forms, so a resolved child in extended form no longer reports as inside a
root in normal form — reads fail with "outside root directory".

``WindowsCompatFilesystemBackend`` (selected by
``workspace_filesystem_backend`` on Windows, the same helper
``build_stage_agent`` routes through) normalizes both sides before the
containment check. The behavioral test below uses an extended-form
``root_dir`` to construct a mixed extended/normal comparison
deterministically — reproducing the failure mode without depending on
machine-level long-path settings. The remaining tests are platform-neutral:
without an extended prefix the normalization is a no-op.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agents.runtime.filesystem_adapters import (
    WindowsCompatFilesystemBackend,
    workspace_filesystem_backend,
)


def _content_of(read_result: object) -> str:
    file_data = getattr(read_result, "file_data", None)
    assert isinstance(file_data, dict) and "content" in file_data, read_result
    return str(file_data["content"])


def test_workspace_backend_selection_matches_platform(tmp_path: Path) -> None:
    """The factory routes /workspace/ through this helper; on Windows it must
    select the compat subclass, other platforms the stock backend."""

    backend = workspace_filesystem_backend(str(tmp_path))
    if os.name == "nt":
        assert isinstance(backend, WindowsCompatFilesystemBackend)
    else:
        from deepagents.backends import FilesystemBackend

        assert type(backend) is FilesystemBackend


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path behavior")
def test_extended_form_root_dir_reads_work(tmp_path: Path) -> None:
    """A workspace root given in extended form must still serve reads.

    With an extended ``root_dir`` the backend's cwd is the extended path while
    a plain absolute path passed to ``_to_virtual_path`` resolves to the
    normal form: the exact mixed-form comparison that breaks upstream's
    containment check. The compat backend normalizes both sides, so reads and
    virtual-path conversion succeed.
    """

    (tmp_path / "leaf.txt").write_text("payload", encoding="utf-8")
    extended_root = "\\\\?\\" + str(tmp_path)

    compat = WindowsCompatFilesystemBackend(root_dir=extended_root, virtual_mode=True)

    read_result = compat.read("/leaf.txt")
    assert read_result.error is None
    assert _content_of(read_result) == "payload"

    normal_file = tmp_path / "leaf.txt"
    assert compat._to_virtual_path(normal_file) == "/leaf.txt"


def test_compat_backend_keeps_virtual_containment(tmp_path: Path) -> None:
    """Normalization must not weaken the containment guarantees: traversal
    and outside-root paths are still rejected."""

    compat = WindowsCompatFilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)

    with pytest.raises(ValueError, match="Path traversal not allowed"):
        compat._resolve_path("/../escape")

    outside = tmp_path.parent / "compat-outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="outside root directory"):
        compat._resolve_path(str(outside))


def test_non_virtual_mode_delegates_to_upstream(tmp_path: Path) -> None:
    """The compat override only guards virtual-mode backends; non-virtual
    resolution keeps the upstream implementation."""

    compat = WindowsCompatFilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)

    resolved = compat._resolve_path(str(tmp_path / "leaf.txt"))
    assert resolved == (tmp_path / "leaf.txt").resolve()
