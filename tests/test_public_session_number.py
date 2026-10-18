"""Public numbered start and attachment input contracts."""

import pytest
from pydantic import ValidationError

from terminal_mcp.mcp.access_contracts import AttachInput, IssuerSessionInput


def test_attach_preserves_leading_zeroes_in_the_public_session_number():
    assert AttachInput.model_validate({"session_number": "0042"}).session_number == "0042"


@pytest.mark.parametrize("number", [None, "0042"])
def test_start_accepts_an_optional_existing_session_number(number):
    request = IssuerSessionInput.model_validate({"action": "start", "session_number": number})

    assert request.session_number == number


@pytest.mark.parametrize("number", ["42", "12345", "ab42", 42])
def test_start_rejects_a_number_outside_the_four_digit_contract(number):
    with pytest.raises(ValidationError):
        IssuerSessionInput.model_validate({"action": "start", "session_number": number})


def test_end_rejects_an_explicit_session_number():
    with pytest.raises(ValidationError):
        IssuerSessionInput.model_validate({"action": "end", "session_number": "0042"})
