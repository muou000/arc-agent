"""write_file/edit_file success receipts must carry write-integrity ground truth.

A long tool call's arguments are truncated in the model's context echo as
``...(argument truncated)`` (same source as debug.log). Models misread that
marker as "my arguments were cut off, the file now holds a truncated
placeholder" — easy-ticketbooking 2026-09-21: TestGenerator burned ~11 minutes
on a delete-rewrite loop plus per-path budget workarounds triggered purely by
the misread, while debug.log proved the write had landed in full.

``ARCFilesystemMiddleware`` (wired by ``build_stage_agent``; it replaces the
stock middleware by name) therefore appends an integrity trailer to the
*successful* receipts of write_file and edit_file: ``bytes_written`` and the
first 8 hex chars of the content's sha256 — ground truth the model can check
against what it sent without re-reading the file — plus a standing note that
the echo marker is display truncation only. Error receipts keep upstream's
exact text.

Issue #247 adds the change region to the same trailer: a successful edit_file
receipt states ``changed_lines: A-B`` (computed by comparing the pre-edit and
post-edit on-disk reads, not inferred from the receipt) plus a short excerpt
of the region's final lines, so a model that wants to confirm its edit has
the answer in the receipt instead of re-reading the whole file (a re-read the
post-write budget then blocks). A successful write_file replaces the whole
file, so its region is ``changed_lines: 1-N``. Region lines degrade honestly:
an unreadable pre-edit state (or a no-op edit) omits them rather than guessing.

Two assertion layers, mirroring ``test_delete_not_found_precedence.py``:

- Build-path probes drive a real ``build_stage_agent`` deep agent with a
  scripted model, asserting the receipts the model actually receives.
- Adapter probes drive ``ARCFilesystemMiddleware`` directly for the sync/async
  parity and degraded-read-back shapes.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any

from agents.runtime.filesystem_adapters import ARCFilesystemMiddleware, workspace_filesystem_backend
from tests.helpers.faux import drive_scripted_tool_call

_BYTES_LINE = re.compile(r"^bytes_written: (\d+)$", re.MULTILINE)
_SHA_LINE = re.compile(r"^sha256: ([0-9a-f]{8})$", re.MULTILINE)
_CHANGED_LINES_LINE = re.compile(r"^changed_lines: (\d+)-(\d+)$", re.MULTILINE)
_EXCERPT_HEADER = "changed_excerpt:"

# Stable fragments of the standing note; the full wording may be tuned but
# both ideas — echo truncation is display-only, written content unaffected —
# must survive in every success receipt.
_NOTE_ECHO_FRAGMENT = "argument truncated"
_NOTE_DISPLAY_FRAGMENT = "display truncation"


def _expected_sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Build-path probes: what the model receives from a built stage agent
# ---------------------------------------------------------------------------


def test_write_file_receipt_carries_integrity_trailer(tmp_project_dir: Path) -> None:
    content = "export const answer = 42;\n"

    (receipt,) = drive_scripted_tool_call(
        tmp_project_dir,
        "write_file",
        {"file_path": "/workspace/src/app.py", "content": content},
        stage="implementation",
    )

    assert receipt.startswith("Updated file /workspace/src/app.py")
    assert _BYTES_LINE.search(receipt).group(1) == str(len(content.encode("utf-8")))
    assert _SHA_LINE.search(receipt).group(1) == _expected_sha(content)
    assert _NOTE_ECHO_FRAGMENT in receipt
    assert _NOTE_DISPLAY_FRAGMENT in receipt
    # The ground truth describes the file that is actually on disk.
    assert (tmp_project_dir / "src" / "app.py").read_text(encoding="utf-8") == content


def test_write_file_receipt_counts_utf8_bytes_not_chars(tmp_project_dir: Path) -> None:
    content = "中文\n"

    (receipt,) = drive_scripted_tool_call(
        tmp_project_dir,
        "write_file",
        {"file_path": "/workspace/src/i18n.txt", "content": content},
        stage="implementation",
    )

    # 2 CJK chars (3 bytes each) + newline: 7 utf-8 bytes, 3 characters.
    assert _BYTES_LINE.search(receipt).group(1) == "7"
    assert _SHA_LINE.search(receipt).group(1) == _expected_sha(content)


def test_write_file_receipt_truth_survives_literal_truncation_marker(
    tmp_project_dir: Path,
) -> None:
    """Content that itself contains the echo marker must not poison the receipt."""

    content = "data = '...(argument truncated)'\n"

    (receipt,) = drive_scripted_tool_call(
        tmp_project_dir,
        "write_file",
        {"file_path": "/workspace/src/literal.py", "content": content},
        stage="implementation",
    )

    assert _BYTES_LINE.search(receipt).group(1) == str(len(content.encode("utf-8")))
    assert _SHA_LINE.search(receipt).group(1) == _expected_sha(content)
    assert (tmp_project_dir / "src" / "literal.py").read_text(encoding="utf-8") == content


def test_edit_file_receipt_carries_integrity_trailer(tmp_project_dir: Path) -> None:
    target = tmp_project_dir / "tests" / "greeting.spec.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"hello TARGET world\n")

    (receipt,) = drive_scripted_tool_call(
        tmp_project_dir,
        "edit_file",
        {
            "file_path": "/workspace/tests/greeting.spec.ts",
            "old_string": "TARGET",
            "new_string": "REPLACED",
        },
        stage="implementation",
    )

    assert receipt.startswith("Successfully replaced 1 instance(s) of the string in")
    edited = target.read_bytes()
    assert edited == b"hello REPLACED world\n"
    assert _BYTES_LINE.search(receipt).group(1) == str(len(edited))
    assert _SHA_LINE.search(receipt).group(1) == _expected_sha(edited.decode("utf-8"))
    assert _NOTE_ECHO_FRAGMENT in receipt
    assert _NOTE_DISPLAY_FRAGMENT in receipt


def test_edit_file_receipt_matches_lf_rewritten_disk_bytes(tmp_project_dir: Path) -> None:
    """Upstream edit rewrites the whole file LF-only; the trailer hashes exactly that.

    This pins the byte-faithfulness assumption behind hashing the read-back:
    a CRLF file is universal-newline-read, replaced, and rewritten with
    ``newline=""``, so the post-edit disk bytes are the LF-normalized text.
    """

    target = tmp_project_dir / "tests" / "crlf.spec.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"a\r\nTARGET\r\nb\r\n")

    (receipt,) = drive_scripted_tool_call(
        tmp_project_dir,
        "edit_file",
        {
            "file_path": "/workspace/tests/crlf.spec.ts",
            "old_string": "TARGET",
            "new_string": "X",
        },
        stage="implementation",
    )

    assert target.read_bytes() == b"a\nX\nb\n"
    assert _BYTES_LINE.search(receipt).group(1) == "6"
    assert _SHA_LINE.search(receipt).group(1) == _expected_sha("a\nX\nb\n")
    # The region compares the universal-newline pre-read against the LF-only
    # post-edit disk state: CRLF before the edit must not smear the span.
    match = _CHANGED_LINES_LINE.search(receipt)
    assert match, receipt
    assert (match.group(1), match.group(2)) == ("2", "2")
    assert f"{_EXCERPT_HEADER}\n    X\n" in receipt


def test_edit_to_whitespace_only_file_degrades_to_note_only_trailer(
    tmp_project_dir: Path,
) -> None:
    """A whitespace-only result has no faithful read-back window (the backend
    answers with a reminder string, no pagination metadata), so the receipt
    keeps the note and drops the bytes/hash lines instead of lying about them.
    """

    target = tmp_project_dir / "tests" / "blank.spec.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"content\n")

    (receipt,) = drive_scripted_tool_call(
        tmp_project_dir,
        "edit_file",
        {
            "file_path": "/workspace/tests/blank.spec.ts",
            "old_string": "content\n",
            "new_string": "   ",
        },
        stage="implementation",
    )

    assert receipt.startswith("Successfully replaced 1 instance(s) of the string in")
    assert "bytes_written" not in receipt
    assert "sha256" not in receipt
    assert _NOTE_ECHO_FRAGMENT in receipt
    assert _NOTE_DISPLAY_FRAGMENT in receipt


def test_edit_file_receipt_reports_changed_lines_and_excerpt(tmp_project_dir: Path) -> None:
    """A successful edit states where the change landed, with a short excerpt.

    The region comes from comparing the pre-edit and post-edit disk reads, so
    it describes the file that is actually there now — the model can confirm
    the edit from the receipt instead of re-reading the file (a re-read the
    post-write budget blocks).
    """

    target = tmp_project_dir / "src" / "handler.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"line1\nline2\nline3\nline4\n")

    (receipt,) = drive_scripted_tool_call(
        tmp_project_dir,
        "edit_file",
        {
            "file_path": "/workspace/src/handler.ts",
            "old_string": "line2",
            "new_string": "LINE-2",
        },
        stage="implementation",
    )

    match = _CHANGED_LINES_LINE.search(receipt)
    assert match, receipt
    assert (match.group(1), match.group(2)) == ("2", "2")
    # The excerpt shows the final composed line at the reported location.
    assert f"{_EXCERPT_HEADER}\n    LINE-2\n" in receipt


def test_edit_file_receipt_excerpt_caps_large_regions(tmp_project_dir: Path) -> None:
    """A multi-line replacement shows the span plus the first excerpt lines."""

    target = tmp_project_dir / "src" / "big.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"a\nb\nc\n")

    (receipt,) = drive_scripted_tool_call(
        tmp_project_dir,
        "edit_file",
        {
            "file_path": "/workspace/src/big.ts",
            "old_string": "b",
            "new_string": "B1\nB2\nB3\nB4\nB5",
        },
        stage="implementation",
    )

    match = _CHANGED_LINES_LINE.search(receipt)
    assert match, receipt
    assert (match.group(1), match.group(2)) == ("2", "6")
    assert f"{_EXCERPT_HEADER}\n    B1\n    B2\n    B3\n    …\n" in receipt


def test_edit_file_receipt_deletion_reports_the_seam_line(tmp_project_dir: Path) -> None:
    """A pure deletion leaves no new text to excerpt; the seam line is reported."""

    target = tmp_project_dir / "src" / "drop.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"alpha\nbeta\ngamma\n")

    (receipt,) = drive_scripted_tool_call(
        tmp_project_dir,
        "edit_file",
        {
            "file_path": "/workspace/src/drop.ts",
            "old_string": "beta\n",
            "new_string": "",
        },
        stage="implementation",
    )

    assert target.read_bytes() == b"alpha\ngamma\n"
    match = _CHANGED_LINES_LINE.search(receipt)
    assert match, receipt
    assert (match.group(1), match.group(2)) == ("2", "2")
    assert f"{_EXCERPT_HEADER}\n    gamma\n" in receipt


def test_edit_file_failure_receipt_has_no_region(tmp_project_dir: Path) -> None:
    """A failed edit (anchor absent) keeps upstream's error text: no region,
    no integrity lines, no note — nothing that reads as if the edit landed."""

    target = tmp_project_dir / "src" / "absent.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"unchanged\n")

    (receipt,) = drive_scripted_tool_call(
        tmp_project_dir,
        "edit_file",
        {
            "file_path": "/workspace/src/absent.ts",
            "old_string": "NOT-PRESENT",
            "new_string": "X",
        },
        stage="implementation",
    )

    assert "changed_lines" not in receipt
    assert "changed_excerpt" not in receipt
    assert "bytes_written" not in receipt
    assert _NOTE_DISPLAY_FRAGMENT not in receipt
    assert target.read_bytes() == b"unchanged\n"


def test_write_file_receipt_reports_whole_file_range(tmp_project_dir: Path) -> None:
    """A write replaces the entire file, so its change region is 1-N."""

    content = "one\ntwo\nthree\n"

    (receipt,) = drive_scripted_tool_call(
        tmp_project_dir,
        "write_file",
        {"file_path": "/workspace/src/whole.py", "content": content},
        stage="implementation",
    )

    match = _CHANGED_LINES_LINE.search(receipt)
    assert match, receipt
    assert (match.group(1), match.group(2)) == ("1", "3")
    assert "changed_excerpt" not in receipt


def test_write_file_receipt_empty_content_has_no_region(tmp_project_dir: Path) -> None:
    """An empty write has no lines to span; the region line is omitted."""

    (receipt,) = drive_scripted_tool_call(
        tmp_project_dir,
        "write_file",
        {"file_path": "/workspace/src/empty.py", "content": ""},
        stage="implementation",
    )

    assert "changed_lines" not in receipt
    assert _BYTES_LINE.search(receipt).group(1) == "0"


def test_error_receipts_keep_upstream_text_untouched(tmp_project_dir: Path) -> None:
    """Denied paths keep the plain permission-denied receipt: no trailer lines,
    no note — failure text is matched by log scanners and stage discipline."""

    (receipt,) = drive_scripted_tool_call(
        tmp_project_dir,
        "write_file",
        {"file_path": "/workspace/.git/protected.py", "content": "x = 1\n"},
        stage="implementation",
    )

    assert "permission denied" in receipt
    assert "bytes_written" not in receipt
    assert "sha256" not in receipt
    assert _NOTE_DISPLAY_FRAGMENT not in receipt


# ---------------------------------------------------------------------------
# Adapter probes: sync/async parity and degraded read-backs
# ---------------------------------------------------------------------------


def _make_middleware(tmp_project_dir: Path) -> tuple[ARCFilesystemMiddleware, Any]:
    """Build the production adapter over ARC's real backend routes and permissions.

    Mirrors the factory's construction (same backend helper, same permission
    rules) and also returns the inner workspace backend so tests can degrade
    its read path without touching the write/edit path.
    """

    from agents.runtime.factory import _build_filesystem_permissions
    from deepagents.backends import CompositeBackend, StateBackend

    root = tmp_project_dir.resolve()
    inner = workspace_filesystem_backend(str(root))
    backend = CompositeBackend(
        default=StateBackend(),
        routes={"/workspace/": inner},
    )
    permissions = _build_filesystem_permissions(
        root,
        [str(root)],
        skill_instruction_paths=[],
    )
    return ARCFilesystemMiddleware(backend=backend, _permissions=permissions), inner


def _middleware_tool(middleware: ARCFilesystemMiddleware, name: str) -> Any:
    return {tool.name: tool for tool in middleware.tools}[name]


def _runtime() -> Any:
    from langgraph.prebuilt.tool_node import ToolRuntime

    return ToolRuntime(
        state={},
        context=None,
        config={},
        stream_writer=lambda *_: None,
        tool_call_id="call-receipt-1",
        store=None,
        tools=[],
    )


def test_write_file_receipt_async_matches_sync(tmp_project_dir: Path) -> None:
    middleware, _ = _make_middleware(tmp_project_dir)
    tool = _middleware_tool(middleware, "write_file")

    sync_message = tool.func(file_path="/workspace/a.py", content="x = 1\n", runtime=_runtime())
    async_message = asyncio.run(
        tool.coroutine(file_path="/workspace/b.py", content="x = 1\n", runtime=_runtime())
    )

    assert sync_message.content == async_message.content.replace("/workspace/b.py", "/workspace/a.py")


def test_edit_file_receipt_async_matches_sync(tmp_project_dir: Path) -> None:
    for name in ("a.py", "b.py"):
        path = tmp_project_dir / name
        path.write_bytes(b"hello TARGET\n")

    middleware, _ = _make_middleware(tmp_project_dir)
    tool = _middleware_tool(middleware, "edit_file")

    sync_message = tool.func(
        file_path="/workspace/a.py",
        old_string="TARGET",
        new_string="X",
        runtime=_runtime(),
    )
    async_message = asyncio.run(
        tool.coroutine(
            file_path="/workspace/b.py",
            old_string="TARGET",
            new_string="X",
            runtime=_runtime(),
        )
    )

    assert sync_message.content == async_message.content.replace("/workspace/b.py", "/workspace/a.py")


def test_edit_receipt_degrades_to_note_when_read_back_fails(
    tmp_project_dir: Path,
    monkeypatch: Any,
) -> None:
    """A crashing read-back must not fail the (already successful) edit."""

    target = tmp_project_dir / "a.py"
    target.write_bytes(b"hello TARGET\n")

    middleware, inner = _make_middleware(tmp_project_dir)
    tool = _middleware_tool(middleware, "edit_file")

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("transient read failure")

    monkeypatch.setattr(inner, "read", _boom)

    message = tool.func(
        file_path="/workspace/a.py",
        old_string="TARGET",
        new_string="X",
        runtime=_runtime(),
    )

    assert message.status == "success"
    assert message.content.startswith("Successfully replaced 1 instance(s) of the string in")
    assert "bytes_written" not in message.content
    assert _NOTE_ECHO_FRAGMENT in message.content
    assert _NOTE_DISPLAY_FRAGMENT in message.content
    # The edit itself still landed.
    assert target.read_bytes() == b"hello X\n"


def test_edit_receipt_pre_read_failure_keeps_integrity_drops_region(
    tmp_project_dir: Path,
    monkeypatch: Any,
) -> None:
    """An unreadable pre-edit state degrades to no region lines — a guessed
    span would be a lying receipt. The post-edit integrity lines survive."""

    target = tmp_project_dir / "a.py"
    target.write_bytes(b"hello TARGET\n")

    middleware, inner = _make_middleware(tmp_project_dir)
    tool = _middleware_tool(middleware, "edit_file")

    real_read = inner.read
    calls = {"n": 0}

    def _flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient pre-read failure")
        return real_read(*args, **kwargs)

    monkeypatch.setattr(inner, "read", _flaky)

    message = tool.func(
        file_path="/workspace/a.py",
        old_string="TARGET",
        new_string="X",
        runtime=_runtime(),
    )

    assert message.status == "success"
    assert _BYTES_LINE.search(message.content), message.content
    assert "changed_lines" not in message.content
    assert "changed_excerpt" not in message.content
    assert target.read_bytes() == b"hello X\n"


def test_parallel_edit_batch_keeps_regions_per_path(tmp_project_dir: Path) -> None:
    """Concurrent edits to different paths each report their own file's region.

    The pre/post reads are threaded through one invocation's locals — no
    middleware-level cache keyed by path that a parallel batch could
    cross-contaminate.
    """

    (tmp_project_dir / "a.py").write_bytes(b"hello TARGET world\n")
    (tmp_project_dir / "b.py").write_bytes(b"one\ntwo\nthree\n")

    middleware, _ = _make_middleware(tmp_project_dir)
    tool = _middleware_tool(middleware, "edit_file")

    async def _drive() -> list[Any]:
        return await asyncio.gather(
            tool.coroutine(
                file_path="/workspace/a.py",
                old_string="TARGET",
                new_string="REPLACED",
                runtime=_runtime(),
            ),
            tool.coroutine(
                file_path="/workspace/b.py",
                old_string="three",
                new_string="3",
                runtime=_runtime(),
            ),
        )

    first, second = asyncio.run(_drive())

    first_match = _CHANGED_LINES_LINE.search(first.content)
    second_match = _CHANGED_LINES_LINE.search(second.content)
    assert first_match, first.content
    assert second_match, second.content
    assert (first_match.group(1), first_match.group(2)) == ("1", "1")
    assert f"{_EXCERPT_HEADER}\n    hello REPLACED world\n" in first.content
    assert (second_match.group(1), second_match.group(2)) == ("3", "3")
    assert f"{_EXCERPT_HEADER}\n    3\n" in second.content
    assert (tmp_project_dir / "a.py").read_bytes() == b"hello REPLACED world\n"
    assert (tmp_project_dir / "b.py").read_bytes() == b"one\ntwo\n3\n"
