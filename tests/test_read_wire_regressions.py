"""Wire-size and lossless-pagination regressions independent of application adapters."""

import json

import pytest

from terminal_mcp.core.read_contract import (
    CALL_TOOL_RESULT_BUDGET_BYTES,
    ITEMS_BUDGET_BYTES,
    MESSAGE_PREVIEW_CHARS,
    bound_rendered_lines,
    summary_message,
)
from terminal_mcp.mcp.output_contracts import cmd_result, serialized_call_tool_result_size
from terminal_mcp.mcp.server import _finish_cmd_read_page


@pytest.mark.parametrize("length", [0, 511, 512, 513, 514, 4096])
def test_message_preview_reports_actual_loss_at_boundary(length):
    result = summary_message({"text": "x" * length})
    assert result["truncated"] is (length > MESSAGE_PREVIEW_CHARS)
    assert result["text"] == (
        "x" * length if length <= MESSAGE_PREVIEW_CHARS else "x" * MESSAGE_PREVIEW_CHARS + "…"
    )


@pytest.mark.parametrize("unit", ['"', "\\", "\x00", "\n", "\t", "Ж", "😀"])
def test_escaped_command_pages_stay_readable_through_final_mcp_envelope(unit):
    all_lines = [f"{i:03}:" + unit * 1200 for i in range(40)]
    seen = []
    cursor = None
    while len(seen) < len(all_lines):
        start = len(seen)
        page = _finish_cmd_read_page(
            {
                "ok": True,
                "cmd_hash": "wire-command",
                "status": "completed",
                "exit_code": 0,
                "lines": all_lines[start:],
                "overall_lines_count": len(all_lines),
            },
            start=start,
            scope={"kind": "test-wire"},
        )
        result = cmd_result(page, "read")
        assert result.structuredContent["ok"] is True
        assert serialized_call_tool_result_size(result) <= CALL_TOOL_RESULT_BUDGET_BYTES
        assert page["lines"]
        assert not page.get("line_truncated", False)
        seen.extend(page["lines"])
        cursor = page["next_cursor"]
        assert bool(cursor) is (len(seen) < len(all_lines))
    assert seen == all_lines
    assert cursor is None


@pytest.mark.parametrize("unit", ['"', "\\", "\x00", "Ж", "😀"])
def test_inherently_oversized_line_is_clipped_by_serialized_size(unit):
    page = _finish_cmd_read_page(
        {
            "ok": True,
            "cmd_hash": "wire-command",
            "status": "completed",
            "exit_code": 0,
            "lines": [unit * (ITEMS_BUDGET_BYTES * 2)],
            "overall_lines_count": 1,
        },
        start=0,
        scope={"kind": "test-wire"},
    )
    assert page["line_truncated"] is True
    assert len(json.dumps(page["lines"], ensure_ascii=False).encode()) <= ITEMS_BUDGET_BYTES
    result = cmd_result(page, "read")
    assert result.structuredContent["ok"] is True
    assert serialized_call_tool_result_size(result) <= CALL_TOOL_RESULT_BUDGET_BYTES
    assert page["next_cursor"] is None


def test_oversized_line_is_deferred_instead_of_clipped_to_page_remainder():
    first = "prefix" * 100
    oversized = "Ж" * (ITEMS_BUDGET_BYTES * 2)
    page, truncated = bound_rendered_lines([first, oversized])
    assert page == [first]
    assert truncated is False
    next_page, next_truncated = bound_rendered_lines([oversized])
    assert len(next_page) == 1
    assert next_truncated is True
    assert len(next_page[0]) > 100


def test_cmd_read_compacts_repeated_long_coordination_inbox_within_wire_budget():
    inbox = [
        {
            "message_hash": f"message-{index:02}",
            "sender": "Peer",
            "text": "x" * 8000,
            "mode": "notify",
            "state": "delivered",
            "created_at": "2026-10-06T00:00:00Z",
        }
        for index in range(12)
    ]
    result = cmd_result(
        {
            "ok": True,
            "cmd_hash": "deadbeef",
            "status": "completed",
            "lines": ["ok"],
            "overall_lines_count": 1,
            "displayed_lines_count": 1,
            "messages": inbox,
            "pending_messages": inbox,
            "ack_required_pending": False,
            "alert_pending": False,
        },
        "read",
    )

    assert result.isError is False
    assert result.structuredContent["ok"] is True
    data = result.structuredContent
    assert "coordination" not in data and "pending_messages" not in data
    assert [item["id"] for item in data["messages"]] == [
        "message-00",
        "message-01",
        "message-02",
        "message-03",
    ]
    assert len({item["id"] for item in data["messages"]}) == 4
    assert data["messages_remaining"] == 8  # Aliases do not double the inbox count.
    assert all(set(item) == {"id", "sender", "text"} for item in data["messages"])
    assert all(item["sender"] == "Peer" for item in data["messages"])
    assert all(len(item["text"]) == MESSAGE_PREVIEW_CHARS + 1 for item in data["messages"])
    assert [warning["code"] for warning in data["warnings"]] == ["incoming_messages_truncated"]
    assert data["command"]["status"] == "completed"
    assert data["lines"] == ["ok"]
    assert json.loads(result.content[0].text) == data
    assert serialized_call_tool_result_size(result) <= CALL_TOOL_RESULT_BUDGET_BYTES


def test_cmd_read_preserves_pending_messages_not_emitted_from_messages_cap():
    messages = [
        {
            "message_hash": f"m-{index}",
            "sender": "Peer",
            "text": "x" * 8000,
            "mode": "notify",
            "state": "delivered",
            "created_at": "2026-10-06T00:00:00Z",
        }
        for index in range(12)
    ]
    pending = messages[8:12]
    result = cmd_result(
        {
            "ok": True,
            "cmd_hash": "deadbeef",
            "status": "completed",
            "lines": ["ok"],
            "overall_lines_count": 1,
            "displayed_lines_count": 1,
            "messages": messages,
            "pending_messages": pending,
            "ack_required_pending": False,
            "alert_pending": False,
        },
        "read",
    )

    data = result.structuredContent
    assert "coordination" not in data and "pending_messages" not in data
    assert [item["id"] for item in data["messages"]] == ["m-0", "m-1", "m-2", "m-3"]
    assert data["messages_remaining"] == 8  # m-8..m-11 are counted once, not lost.
    assert data["command"]["cmd_hash"] == "deadbeef"
    assert data["command"]["status"] == "completed"
    assert data["lines"] == ["ok"]
    assert serialized_call_tool_result_size(result) <= CALL_TOOL_RESULT_BUDGET_BYTES

    assert all(item["state"] == "delivered" and item["text"] == "x" * 8000 for item in messages)

    # A pending-only source still surfaces all four incoming obligations, with their
    # actual action. The outer preview cap cannot erase or acknowledge stored entries.
    for index, item in enumerate(pending):
        item["mode"] = "ack" if index % 2 == 0 else "alert"
    pending_result = cmd_result(
        {
            "ok": True,
            "cmd_hash": "deadbeef",
            "status": "completed",
            "lines": ["ok"],
            "pending_messages": pending,
        },
        "read",
    )
    pending_data = pending_result.structuredContent
    assert [item["id"] for item in pending_data["messages"]] == ["m-8", "m-9", "m-10", "m-11"]
    assert [item["requirements"] for item in pending_data["messages"]] == [
        "ack",
        "reply",
        "ack",
        "reply",
    ]
    assert "messages_remaining" not in pending_data
    assert all(len(item["text"]) <= MESSAGE_PREVIEW_CHARS + 1 for item in pending_data["messages"])
    assert serialized_call_tool_result_size(pending_result) <= CALL_TOOL_RESULT_BUDGET_BYTES
