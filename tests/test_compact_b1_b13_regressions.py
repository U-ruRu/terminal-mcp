"""Current message-input behavior retained from the compact contract review."""

from terminal_mcp.mcp.access_contracts import MeshMessageInput


def test_role_message_explicit_null_mode_is_accepted():
    request = MeshMessageInput.model_validate(
        {
            "action": "send",
            "text": "ok",
            "mode": None,
            "require_reply": False,
            "alert": False,
        }
    )
    assert request.mode is None
