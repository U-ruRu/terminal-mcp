"""Wire-size and lossless-pagination regressions independent of application adapters."""

import json

import pytest

from terminal_mcp.application.projections import _finish_cmd_read_page
from terminal_mcp.core.read_contract import (
    CALL_TOOL_RESULT_BUDGET_BYTES,
    ITEMS_BUDGET_BYTES,
    MESSAGE_PREVIEW_CHARS,
    bound_rendered_lines,
    summary_message,
)
from terminal_mcp.mcp.output_contracts import cmd_result, serialized_call_tool_result_size


@pytest.mark.parametrize("length", [512, 513])
def test_message_preview_reports_actual_loss_at_boundary(length):
    result = summary_message({"text": "x" * length})
    assert result["truncated"] is (length > MESSAGE_PREVIEW_CHARS)
    assert result["text"] == (
        "x" * length if length <= MESSAGE_PREVIEW_CHARS else "x" * MESSAGE_PREVIEW_CHARS + "…"
    )


@pytest.mark.parametrize("unit", ["\x00", "😀"])
def test_escaped_command_page_preserves_lines_within_the_final_wire_budget(unit):
    all_lines = [f"{i:03}:" + unit * 1200 for i in range(12)]

    page = _finish_cmd_read_page(
        {
            "ok": True,
            "cmd_hash": "wire-command",
            "status": "completed",
            "exit_code": 0,
            "lines": all_lines,
            "overall_lines_count": len(all_lines),
        },
        start=0,
        scope={"kind": "test-wire"},
    )

    assert page["lines"] == all_lines[: len(page["lines"])]
    assert page["lines"] and page["next_cursor"]
    assert not page.get("line_truncated", False)
    assert (
        serialized_call_tool_result_size(cmd_result(page, "read")) <= CALL_TOOL_RESULT_BUDGET_BYTES
    )


@pytest.mark.parametrize("unit", ['"', "😀"])
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
