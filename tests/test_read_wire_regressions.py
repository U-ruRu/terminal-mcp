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
    coordination = result.structuredContent["coordination"]
    assert len(coordination["messages"]) == 4
    assert coordination["pending_messages"] == []
    assert all(item["truncated"] is True for item in coordination["messages"])
    assert all(len(item["text"]) == MESSAGE_PREVIEW_CHARS + 1 for item in coordination["messages"])

    legacy = json.loads(result.content[0].text)
    assert legacy["status"] == "completed"
    assert "coordination" not in legacy
    assert legacy["messages"] == coordination["messages"]
    assert legacy["pending_messages"] == []
    assert serialized_call_tool_result_size(result) <= CALL_TOOL_RESULT_BUDGET_BYTES
