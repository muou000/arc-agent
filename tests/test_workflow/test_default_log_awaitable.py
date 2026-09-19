from __future__ import annotations

import asyncio

from core.workflow import _CompletedLogAwaitable


def test_completed_log_awaitable_is_an_empty_iterator() -> None:
    # asyncio.run() demands a native coroutine on 3.12+; await the awaitable
    # the same way production does.
    async def await_it():
        return await _CompletedLogAwaitable()

    assert asyncio.run(await_it()) is None
