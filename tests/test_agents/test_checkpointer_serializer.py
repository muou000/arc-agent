from __future__ import annotations

import logging

from agents.interface_designer import InterfaceDesignResponse
from agents.runtime.checkpointer import get_checkpointer, reset_checkpointer
from agents.test_generator import TestGenerationResponse


def test_checkpointer_allowlists_arc_stage_response_types(caplog) -> None:
    reset_checkpointer()
    checkpointer = get_checkpointer()
    assert checkpointer is not None

    with caplog.at_level(logging.WARNING, logger="langgraph.checkpoint.serde.jsonplus"):
        for response in (
            InterfaceDesignResponse(summary="design"),
            TestGenerationResponse(summary="tests"),
        ):
            kind, payload = checkpointer.serde.dumps_typed(response)
            restored = checkpointer.serde.loads_typed((kind, payload))
            assert type(restored) is type(response)

    assert "Deserializing unregistered type" not in caplog.text
