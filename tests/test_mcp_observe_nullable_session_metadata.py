"""Legacy session listings tolerate transitional session metadata."""

import pytest

from terminal_mcp.mcp.output_contracts import observe_result


@pytest.mark.parametrize(
    "metadata",
    [
        {"mode": None, "authority_node_id": None},
        {},
        {"mode": "persistent", "authority_node_id": "secondary"},
    ],
)
def test_session_observe_projects_transitional_metadata(metadata):
    raw = {
        "ok": True,
        "subject": "sessions",
        "sessions": [{"public_name": "probe-13", "session_state": "active", **metadata}],
        "next_cursor": None,
    }

    result = observe_result(raw, "sessions")
    assert result.isError is False
    item = result.structuredContent["sessions"][0]
    assert item["public_name"] == "probe-13"
    assert item.get("mode") == metadata.get("mode")
    assert item.get("authority_node_id") == metadata.get("authority_node_id")
