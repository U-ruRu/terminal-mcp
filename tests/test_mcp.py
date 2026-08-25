import pytest
from mcp.types import CallToolResult

from terminal_mcp.mcp.server import build_mcp


class FakeService:
    async def run(self, cmd):
        return {"ok": True, "cmd_hash": "1234abcd", "error": None}

    async def recovery(self, cmd):
        return {
            "ok": True,
            "cmd_hash": "abcd1234",
            "lines": ["[00:00:00Z] recovery-ready"],
            "overall_lines_count": 1,
            "displayed_lines_count": 1,
            "exit_code": 0,
            "error": None,
            "duration_ms": 1,
        }

    async def read(self, cmd_hash=None, lines_count=500, offset=None):
        return {
            "ok": True,
            "lines": [],
            "next_offset": 0,
            "overall_lines_count": 0 if cmd_hash else None,
            "displayed_lines_count": 0,
            "cmd_hash": cmd_hash,
            "status": "completed" if cmd_hash else None,
            "exit_code": 0 if cmd_hash else None,
            "error": None,
        }

    async def cancel(self, cmd_hash):
        return {"ok": True, "cmd_hash": cmd_hash, "error": None}

    async def health(self, auth_mode):
        return {
            "ok": True,
            "application": "terminal-mcp",
            "storage": "ok",
            "auth_mode": auth_mode,
            "terminal": {
                "ok": True,
                "user": "root",
                "uid": 0,
                "gid": 0,
                "cwd": "/",
                "privilege": "root",
                "shell": "/bin/bash",
                "terminal_user": "root",
                "scheduler": "fifo",
                "parallelism": 1,
                "queue_size": 0,
                "running_commands": [],
            },
        }


def test_mcp_tools_advertise_structured_output_schemas():
    mcp = build_mcp(FakeService())
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    assert set(tools) == {"run", "recovery", "read", "cancel", "health"}
    for tool in tools.values():
        assert tool.output_schema is not None
        assert tool.output_schema["type"] == "object"
        assert tool.annotations is not None
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False
    assert tools["health"].annotations.readOnlyHint is True
    assert tools["read"].annotations.readOnlyHint is True
    assert tools["run"].annotations.readOnlyHint is False
    assert tools["cancel"].annotations.readOnlyHint is False
    assert tools["recovery"].annotations.readOnlyHint is False

    recovery_schema = tools["recovery"].parameters
    assert set(recovery_schema["properties"]) == {"cmd"}
    read_schema = tools["read"].parameters
    assert read_schema["properties"]["lines_count"]["default"] == 500
    assert read_schema["properties"]["lines_count"]["maximum"] == 1000
    assert read_schema["properties"]["offset"]["default"] is None


@pytest.mark.asyncio
async def test_mcp_result_has_summary_and_structured_content():
    mcp = build_mcp(FakeService())
    tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == "run")
    result = await tool.run({"cmd": "printf ok"}, convert_result=True)
    assert isinstance(result, CallToolResult)
    assert result.content[0].text == "Command 1234abcd queued."
    assert result.structuredContent == {
        "ok": True,
        "cmd_hash": "1234abcd",
        "error": None,
    }
    health_tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == "health")
    health_result = await health_tool.run({}, convert_result=True)
    assert "custom_command" not in health_result.structuredContent


@pytest.mark.asyncio
async def test_mcp_recovery_returns_persisted_hash_and_counts():
    mcp = build_mcp(FakeService())
    tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == "recovery")
    result = await tool.run({"cmd": "printf recovery-ready"}, convert_result=True)
    assert isinstance(result, CallToolResult)
    assert result.structuredContent["ok"] is True
    assert result.structuredContent["cmd_hash"] == "abcd1234"
    assert result.structuredContent["overall_lines_count"] == 1
    assert result.structuredContent["displayed_lines_count"] == 1
    assert "status" not in result.structuredContent
    assert result.structuredContent["lines"][0].endswith("recovery-ready")
