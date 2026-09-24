"""The tightened DESIGN response schema (issue #233).

`InterfaceDesignResponse.interfaces` was a loose `list[dict[str, Any]]`; the
serial-5 incident (11 typeless records judged fatal at registration) is the
argument for pushing the record identity down to decode time. These pins hold
the decode contract: identity required, type vocabulary enforced, everything
else lenient — and the tool schema the endpoint sees carries the enum.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agents.interface_designer import InterfaceContractRecord, InterfaceDesignResponse


def _record(**overrides) -> dict:
    row = {"interface_id": "REQ-2-UI-LoginPage", "type": "UI"}
    row.update(overrides)
    return row


def test_decode_rejects_missing_type() -> None:
    with pytest.raises(ValidationError, match="type"):
        InterfaceDesignResponse.model_validate({"interfaces": [{"interface_id": "REQ-2-UI-Login"}]})


def test_decode_rejects_type_outside_vocabulary() -> None:
    with pytest.raises(ValidationError, match="type"):
        InterfaceDesignResponse.model_validate({"interfaces": [_record(type="SERVICE")]})


def test_decode_rejects_empty_interface_id() -> None:
    with pytest.raises(ValidationError, match="interface_id"):
        InterfaceDesignResponse.model_validate({"interfaces": [_record(interface_id="")]})


def test_decode_accepts_canonical_type_and_preserves_everything_else() -> None:
    """Unknown and free-shape fields pass through ``model_dump`` unchanged —
    the stored contract content must not lose them versus the loose-dict
    era (``extra="allow"``). The Literal is case-strict on purpose: the
    decode error names the exact vocabulary, and the registration ladder
    already normalizes case for rows that bypass decode."""

    response = InterfaceDesignResponse.model_validate(
        {
            "summary": "done",
            "interfaces": [
                _record(
                    type="API",
                    inputs={"username": "string"},
                    outputs=[{"code": 200}],
                    test_focus="login rejects bad password",
                    mount_path="/api/login",
                )
            ],
        }
    )
    dumped = response.model_dump()["interfaces"][0]
    assert dumped["type"] == "API"
    assert dumped["inputs"] == {"username": "string"}
    assert dumped["outputs"] == [{"code": 200}]
    assert dumped["test_focus"] == "login rejects bad password"
    assert dumped["mount_path"] == "/api/login"


def test_tool_schema_advertises_the_type_enum() -> None:
    """The JSON schema the structured-output tool sends to the endpoint
    carries the Literal enum — provider-side constrained decoding (where the
    gateway honors it) sees the same vocabulary the client-side decode
    enforces."""

    schema = InterfaceContractRecord.model_json_schema()
    record_schema = schema.get("$defs", {}).get("InterfaceContractRecord", schema)
    assert record_schema["properties"]["type"]["enum"] == ["UI", "API", "FUNC", "DB"]
    assert "type" in record_schema["required"]
    assert "interface_id" in record_schema["required"]
