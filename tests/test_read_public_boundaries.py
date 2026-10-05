import pytest

from terminal_mcp.core.read_contract import (
    CALL_TOOL_RESULT_BUDGET_BYTES,
    READ_RESPONSE_BUDGET_BYTES,
)
from terminal_mcp.mcp.output_contracts import (
    cmd_result,
    serialized_call_tool_result_size,
)
from terminal_mcp.mcp.server import build_mcp


class _CursorOnlyService:
    # A truthy backend is enough for message to reach cursor validation.
    persistent = object()

    async def context(self, *args, **kwargs):
        raise AssertionError("invalid public cursor must be rejected before context backend access")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("observe", {"subject": "sessions", "cursor": "7"}),
        ("message", {"sender": "Alpha", "cursor": "7"}),
        (
            "context",
            {"request": {"action": "list", "cursor": "7"}},
        ),
        (
            "cmd",
            {
                "request": {
                    "action": "read",
                    "cmd_hash": "command-1",
                    "cursor": "7",
                }
            },
        ),
    ],
)
async def test_public_reads_reject_decimal_cursor_bypass(tool_name, arguments):
    tools = {
        tool.name: tool
        for tool in build_mcp(_CursorOnlyService())._tool_manager.list_tools()
    }
    result = await tools[tool_name].run(arguments, convert_result=True)

    assert result.structuredContent["ok"] is False
    assert result.structuredContent["code"] == "invalid_cursor"
    assert serialized_call_tool_result_size(result) <= CALL_TOOL_RESULT_BUDGET_BYTES


class _LargeSessionBackend:
    def __init__(self):
        self.rows = [
            {
                "public_name": f"Agent-{index:03d}",
                "mode": "persistent",
                "display_suffix": "x" * 1200,
                "authority_node_id": "secondary",
                "access_generation": index + 1,
                "session_state": "inactive",
            }
            for index in range(100)
        ]

    async def access_observe_slots(self, *, limit=None, offset=0):
        end = None if limit is None else offset + limit
        return {"ok": True, "sessions": self.rows[offset:end]}


class _LargeSessionService:
    def __init__(self):
        self.persistent = _LargeSessionBackend()


@pytest.mark.asyncio
async def test_public_observe_final_mcp_result_stays_within_serialized_budget():
    tools = {
        tool.name: tool
        for tool in build_mcp(_LargeSessionService())._tool_manager.list_tools()
    }
    result = await tools["observe"].run(
        {
            "subject": "sessions",
            "detail": "full",
            "limit": 100,
        },
        convert_result=True,
    )

    assert result.structuredContent["ok"] is True
    assert result.structuredContent["next_cursor"] is not None
    assert len(result.structuredContent["sessions"]) < 100
    assert serialized_call_tool_result_size(result) <= CALL_TOOL_RESULT_BUDGET_BYTES


def test_final_mcp_budget_covers_large_coordination_metadata():
    result = cmd_result(
        {
            "ok": True,
            "cmd_hash": "command-1",
            "status": "completed",
            "exit_code": 0,
            "lines": [],
            "pending_messages": [
                {
                    "message_hash": "message-1",
                    "sender": "Alpha",
                    "text": "x" * (READ_RESPONSE_BUDGET_BYTES * 2),
                }
            ],
            "messages": [],
            "ack_required_pending": False,
            "alert_pending": False,
        },
        "read",
    )

    assert serialized_call_tool_result_size(result) <= CALL_TOOL_RESULT_BUDGET_BYTES
    assert result.structuredContent["ok"] is False
    assert result.structuredContent["code"] == "output_item_too_large"


class _LargeCmdService:
    async def read(self, cmd_hash=None, lines_count=100, offset=None, agent_id=None):
        start = int(offset or 0)
        all_lines = [f"{index:03d}:" + ("y" * 1200) for index in range(100)]
        lines = all_lines[start : start + int(lines_count)]
        return {
            "ok": True,
            "lines": lines,
            "next_offset": start + len(lines),
            "overall_lines_count": len(all_lines),
            "displayed_lines_count": len(lines),
            "cmd_hash": cmd_hash,
            "status": "completed",
            "exit_code": 0,
            "output_truncated": False,
            "output_retained": True,
            "output_pruned_at": None,
            "output_bytes": sum(len(line.encode("utf-8")) for line in all_lines),
            "error": None,
        }


@pytest.mark.asyncio
async def test_public_cmd_read_final_envelope_is_bounded_with_large_lines():
    tools = {
        tool.name: tool
        for tool in build_mcp(_LargeCmdService())._tool_manager.list_tools()
    }
    result = await tools["cmd"].run(
        {
            "request": {
                "action": "read",
                "cmd_hash": "command-1",
                "limit": 100,
            }
        },
        convert_result=True,
    )

    assert result.structuredContent["ok"] is True
    assert result.structuredContent["has_more"] is True
    assert result.structuredContent["next_cursor"] is not None
    assert len(result.structuredContent["lines"]) < 100
    assert serialized_call_tool_result_size(result) <= CALL_TOOL_RESULT_BUDGET_BYTES
