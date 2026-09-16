"""A backend that fails to start must echo its own console output.

``_start_backend_runtime`` used to attach the backend's stdout/stderr to
``DEVNULL``, so a startup crash (the Express 5 bare-wildcard route throw, a
bad import, a missing module) produced only "Failed to start backend runtime
... within 20 seconds". The TDD repair loop then had no evidence about *why*
the server died and burned its whole retry budget guessing. The failure body
now carries the crashed process's own output.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
from pathlib import Path

import pytest

from app_type_handler import web as web_handler


def _make_backend_workspace(tmp_path: Path, start_script: str) -> Path:
    backend = tmp_path / "backend"
    (backend / "src").mkdir(parents=True)
    (backend / "package.json").write_text(
        '{"name": "backend", "scripts": {"start": "node src/server.js"}}\n',
        encoding="utf-8",
    )
    (backend / "src" / "server.js").write_text(start_script, encoding="utf-8")
    return tmp_path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _require_node() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH; backend startup tests need a real node process")


@pytest.mark.slow
def test_failed_startup_echoes_backend_stderr(tmp_path) -> None:
    """A crashing backend's stack trace must appear in the failure detail."""

    _require_node()
    workspace = _make_backend_workspace(
        tmp_path,
        "console.error('TypeError: Cannot read properties of undefined "
        "(reading \\'type\\')'); process.exit(1);\n",
    )

    async def _run() -> tuple:
        return await web_handler._start_backend_runtime(
            str(workspace), {}, web_port=_free_port()
        )

    process, _command, detail, _fingerprint = asyncio.run(_run())

    assert process is None
    assert "within 20 seconds" in detail
    assert "=== Backend Process Output ===" in detail
    assert "STDERR:" in detail
    assert "TypeError: Cannot read properties of undefined" in detail


@pytest.mark.slow
def test_failed_startup_reports_placeholder_when_no_output(tmp_path) -> None:
    """A process that prints nothing beyond the npm banner reports it."""

    _require_node()
    workspace = _make_backend_workspace(
        tmp_path,
        "setInterval(() => {}, 1000); // never listens, never prints\n",
    )

    async def _run() -> tuple:
        return await web_handler._start_backend_runtime(
            str(workspace), {}, web_port=_free_port()
        )

    process, _command, detail, _fingerprint = asyncio.run(_run())

    assert process is None
    assert "=== Backend Process Output ===" in detail
    # The npm banner ("> start") is the only stdout; no STDERR section.
    assert "STDERR:" not in detail


@pytest.mark.slow
def test_successful_startup_survives_chatty_output(tmp_path) -> None:
    """A serving backend must not block on a full stdout pipe buffer later.

    The runtime stays alive for the whole E2E session with nobody consuming
    its pipes; the background drain added next to the success return keeps a
    chatty server from freezing on its next console.log once the OS pipe
    buffer fills.
    """

    _require_node()
    # ~200KB of logging is beyond any OS pipe buffer; without the drain task
    # the server blocks mid-loop on write and the health probe below fails.
    workspace = _make_backend_workspace(
        tmp_path,
        (
            "const http = require('http');\n"
            "const server = http.createServer((req, res) => { res.end('ok'); });\n"
            "server.listen(process.env.ARC_WEB_PORT || 0, '127.0.0.1', () => {\n"
            "  console.log('listening');\n"
            "  for (let i = 0; i < 200; i++) { console.log('x'.repeat(1024)); }\n"
            "  console.log('FLOOD_DONE');\n"
            "});\n"
        ),
    )
    port = _free_port()

    async def _start_and_probe() -> None:
        process, _command, detail, _fingerprint = await web_handler._start_backend_runtime(
            str(workspace), {"ARC_WEB_PORT": str(port)}, web_port=port
        )
        assert process is not None, detail
        try:
            assert await web_handler._wait_for_http_server("127.0.0.1", port, timeout=10.0)
        finally:
            await web_handler._terminate_process(process, port=port)

    asyncio.run(_start_and_probe())


def test_runtime_contract_warns_about_express5_wildcard_routes() -> None:
    lines = web_handler.WebAppType.runtime_contract_lines(web_port=3301)

    text = "\n".join(lines)
    assert "Express 5" in text
    assert "'*'" in text
    assert "'/{*splat}'" in text


def test_output_tail_drains_are_anchored_to_the_process() -> None:
    """The drain tasks must stay referenced for the process's whole lifetime.

    asyncio keeps only weak references to running tasks: a task whose only
    reference is the local variable in `_start_output_tails` can be
    garbage-collected mid-session, and the pipes fill up again. The drains
    are therefore anchored on the Process object the caller holds.
    """

    import asyncio as _asyncio

    async def _start_and_collect() -> list:
        process = await _asyncio.create_subprocess_exec(
            "cmd", "/c", "echo hi", stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.PIPE
        )
        stdout_tail, stderr_tail = web_handler._start_output_tails(process)
        anchored = getattr(process, "_arc_output_tails", None)
        await process.wait()
        return [stdout_tail, stderr_tail, anchored]

    stdout_tail, stderr_tail, anchored = _asyncio.run(_start_and_collect())

    assert anchored is not None, "drains must be anchored on the Process object"
    anchored_tails, anchored_tasks = anchored[0:2], anchored[2]
    assert stdout_tail in anchored_tails and stderr_tail in anchored_tails
    assert anchored_tasks, "the consuming asyncio tasks must be kept referenced"
    assert all(task.done() or not task.cancelled() for task in anchored_tasks)
    assert stdout_tail.text().startswith("hi")


def test_output_tail_ring_cut_does_not_lead_with_garbage() -> None:
    """A ring-buffer cut splitting a UTF-8 sequence must not prefix U+FFFD.

    The 64KB ring keeps the newest bytes; the cut can split a multi-byte
    character at the buffer head, and the stray replacement character would
    lead the echoed output when the 4KB window reaches the buffer start.
    """

    char = "你".encode("utf-8")  # 3 bytes each
    tail = web_handler._ProcessOutputTail()
    # Simulate the post-cut ring state: the buffer begins with the trailing
    # 2 bytes of a character whose first byte was dropped by the ring cut.
    with tail._lock:
        tail._chunks.extend(char[:2] + char * 100)

    text = tail.text()

    assert not text.startswith("\ufffd")
    assert text.startswith("你")
    assert len(text) == 100


def test_output_tail_cancel_propagates() -> None:
    """Cancelling a drain task must surface as cancellation, not a silent exit."""

    import asyncio as _asyncio

    async def _start_and_cancel() -> bool:
        process = await _asyncio.create_subprocess_exec(
            "cmd",
            "/c",
            "pause > nul",
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
        )
        try:
            _stdout_tail, _stderr_tail = web_handler._start_output_tails(process)
            drains = getattr(process, "_arc_output_tails")[2]
            assert drains
            drains[0].cancel()
            try:
                await _asyncio.wait_for(_asyncio.shield(drains[0]), timeout=5.0)
                return False  # returned normally: cancellation was swallowed
            except _asyncio.CancelledError:
                return True
        finally:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    assert _asyncio.run(_start_and_cancel())


@pytest.mark.slow
def test_terminate_process_awaits_drain_tasks(tmp_path) -> None:
    """Teardown must not leave drain tasks pending after the process is gone.

    `_terminate_process` is the convergence point of every E2E teardown path;
    it awaits the anchored drains (process death closes the pipes, the drains
    see EOF) so a closing event loop never trips over still-pending tasks.
    """

    _require_node()
    workspace = _make_backend_workspace(
        tmp_path,
        (
            "const http = require('http');\n"
            "http.createServer((req, res) => res.end('ok')).listen("
            "process.env.ARC_WEB_PORT, '127.0.0.1');\n"
        ),
    )
    port = _free_port()

    async def _start_terminate_and_check() -> bool:
        process, _command, _detail, _fingerprint = await web_handler._start_backend_runtime(
            str(workspace), {"ARC_WEB_PORT": str(port)}, web_port=port
        )
        assert process is not None
        await web_handler._terminate_process(process, port=port)
        drains = getattr(process, "_arc_output_tails", (None, None, []))[2]
        return all(task.done() for task in drains)

    assert asyncio.run(_start_terminate_and_check())
