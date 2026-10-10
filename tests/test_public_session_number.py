"""Final public contracts: server chooses a number, attach needs number alone."""

import pytest
from pydantic import ValidationError

from terminal_mcp.mcp.access_contracts import AttachInput, IssuerOutput, IssuerSessionInput


def test_access_start_takes_action_only_and_server_issues_number():
    assert IssuerSessionInput.model_validate({"action": "start"}).action == "start"
    assert IssuerSessionInput.model_validate({"action": "end"}).action == "end"
    for args in ({"action":"status"}, {"action":"start","mode":"legacy"},
                 {"action":"start","session_number":"0042"},
                 {"action":"start","code":"0042"}):
        with pytest.raises(ValidationError):
            IssuerSessionInput.model_validate(args)


def test_attach_requires_only_four_digit_session_number():
    good = AttachInput.model_validate({"session_number": "0042"})
    assert good.session_number == "0042"
    for args in ({"session_number":"42"}, {"session_number":"12345"},
                 {"session_number":"04x2"}, {"session_number":42},
                 {"issuer_node_id":"firstbyte","session_number":"0042"},
                 {"access_code":"0042"}, {}):
        with pytest.raises(ValidationError):
            AttachInput.model_validate(args)


def test_access_output_issues_only_session_number_and_ok():
    output = IssuerOutput.model_validate({"ok":True,"session_number":"0042"}).model_dump()
    assert output == {"ok":True,"session_number":"0042"}
    assert "access_code" not in str(IssuerOutput.model_json_schema())
    with pytest.raises(ValidationError):
        IssuerOutput.model_validate({"ok":True,"session_number":"0042","issuer_node_id":"firstbyte"})
