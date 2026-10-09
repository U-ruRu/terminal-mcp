"""Public four-digit session number contract; internal Access storage is untouched."""

import pytest
from pydantic import ValidationError

from terminal_mcp.mcp.access_contracts import AttachInput, IssuerOutput, IssuerSessionInput


def test_start_accepts_four_digit_string_with_leading_zeroes():
    assert IssuerSessionInput.model_validate(
        {"action": "start", "mode": "persistent", "session_number": "0042"}
    ).session_number == "0042"
    assert IssuerSessionInput.model_validate(
        {"action": "start", "mode": "legacy"}
    ).session_number is None
    for bad in ("42", "12345", "12ab", 42):
        with pytest.raises(ValidationError):
            IssuerSessionInput.model_validate(
                {"action": "start", "mode": "persistent", "session_number": bad}
            )
    with pytest.raises(ValidationError):
        IssuerSessionInput.model_validate(
            {"action": "start", "mode": "persistent", "code": "0042"}
        )


def test_attach_requires_exact_four_digits_plus_issuer():
    good = AttachInput.model_validate({"issuer_node_id": "firstbyte", "session_number": "0042"})
    assert good.session_number == "0042"
    for bad in ("42", "12345", "firstbyte:0042", "04x2", 42):
        with pytest.raises(ValidationError):
            AttachInput.model_validate({"issuer_node_id": "firstbyte", "session_number": bad})
    for kwargs in (
        {"session_number": "0042"},
        {"issuer_node_id": "firstbyte", "access_code": "0042"},
    ):
        with pytest.raises(ValidationError):
            AttachInput.model_validate(kwargs)


def test_access_output_exposes_only_session_number_not_access_code():
    result = IssuerOutput.model_validate({
        "ok": True, "action": "start", "issuer_node_id": "firstbyte",
        "public_name": "firstbyte-fixture", "mode": "legacy",
        "session_number": "0042",
        "session_state": "active", "hard_expires_at": None, "remaining_seconds": 100,
    })
    output = result.model_dump(mode="json", exclude_none=True)
    assert output["session_number"] == "0042"
    assert "access_code" not in output
    assert "access_code" not in str(IssuerOutput.model_json_schema())
