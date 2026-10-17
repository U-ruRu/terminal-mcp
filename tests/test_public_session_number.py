"""Final public contracts: server chooses a number, attach needs number alone."""

from terminal_mcp.mcp.access_contracts import AttachInput


def test_attach_preserves_leading_zeroes_in_the_public_session_number():
    assert AttachInput.model_validate({"session_number": "0042"}).session_number == "0042"
