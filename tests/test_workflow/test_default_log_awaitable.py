from __future__ import annotations

import asyncio

from core.workflow import _CompletedLogAwaitable


def test_completed_log_awaitable_is_an_empty_iterator() -> None:
    assert asyncio.run(_CompletedLogAwaitable()) is None
