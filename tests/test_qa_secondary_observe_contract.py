"""Regression for old Secondary SessionSummary rejecting managed entries."""

from terminal_mcp.mcp.output_contracts import ObserveSessionsResult, SessionSummary


def test_managed_session_summary_accepts_missing_mode_and_authority():
    parsed = SessionSummary.model_validate({"public_name": "Bravo"})
    assert parsed.mode is None
    assert parsed.authority_node_id is None


def test_managed_session_summary_accepts_explicit_null_fields():
    parsed = SessionSummary.model_validate(
        {"public_name": "Coordinator", "mode": None, "authority_node_id": None}
    )
    assert parsed.mode is None
    assert parsed.authority_node_id is None


def test_observe_sessions_mixes_legacy_and_managed_entries():
    result = ObserveSessionsResult.model_validate(
        {
            "ok": True,
            "subject": "sessions",
            "sessions": [
                {"public_name": "Bravo"},
                {"public_name": "Alpha", "mode": "persistent", "authority_node_id": "secondary"},
                {"public_name": "Coordinator", "mode": None, "authority_node_id": None},
            ],
            "next_cursor": None,
        }
    )
    assert len(result.sessions) == 3
    assert result.sessions[1].authority_node_id == "secondary"
