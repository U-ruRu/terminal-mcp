import pytest
from mcp.types import CallToolResult

from terminal_mcp.mcp.server import build_mcp


class FakeService:
    async def agent_start(self, task_summary, intent, work_scope):
        return {
            "ok": True,
            "self": {
                "agent_id": "Kilo-7K2M",
                "ttl_seconds": 300,
                "task_lease_seconds": 180,
                "task_summary": task_summary,
                "intent": intent,
                "work_scope": work_scope,
            },
            "active": [],
            "overlaps": [],
            "additional_active_agents": 0,
        }

    async def agent_task(self, agent_id, intent, work_scope, detail=None):
        return {
            "ok": True,
            "self": {
                "agent_id": agent_id,
                "ttl_seconds": 300,
                "task_lease_seconds": 180,
                "task_summary": "task",
                "intent": intent,
                "work_scope": work_scope,
            },
            "active": [],
            "overlaps": [],
            "additional_active_agents": 0,
            "detail": detail,
        }

    async def agents(self, agent_id):
        return {
            "ok": True,
            "self": {
                "agent_id": agent_id,
                "ttl_seconds": 300,
                "task_lease_seconds": 180,
                "task_summary": "task",
                "intent": "work",
                "work_scope": ["repo:mcp"],
            },
            "active": [],
            "overlaps": [],
            "additional_active_agents": 0,
        }

    async def agent_finish(self, agent_id):
        return {"ok": True, "agent_id": agent_id, "finished": True}

    async def run(self, cmd, agent_id=None):
        return {"ok": True, "cmd_hash": "1234abcd", "error": None, "agent_id": agent_id}

    async def recovery(self, cmd, agent_id=None):
        return {
            "ok": True,
            "cmd_hash": "abcd1234",
            "lines": ["[00:00:00Z] recovery-ready"],
            "overall_lines_count": 1,
            "displayed_lines_count": 1,
            "exit_code": 0,
            "error": None,
            "duration_ms": 1,
            "agent_id": agent_id,
        }

    async def read(self, cmd_hash=None, lines_count=500, offset=None, agent_id=None):
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
            "agent_id": agent_id,
        }

    async def cancel(self, cmd_hash, agent_id=None):
        return {"ok": True, "cmd_hash": cmd_hash, "error": None, "agent_id": agent_id}

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


def test_mcp_tools_advertise_agent_protocol_and_structured_schemas():
    mcp = build_mcp(FakeService())
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    assert set(tools) == {
        "agent_start",
        "agent_task",
        "agents",
        "agent_finish",
        "run",
        "recovery",
        "read",
        "cancel",
        "health",
    }
    for tool in tools.values():
        assert tool.output_schema is not None and tool.output_schema["type"] == "object"
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False
    assert tools["agents"].annotations.readOnlyHint is True
    assert tools["health"].annotations.readOnlyHint is True
    assert tools["read"].annotations.readOnlyHint is True
    assert tools["run"].parameters["required"] == ["agent_id", "cmd"]
    assert tools["recovery"].parameters["required"] == ["cmd"]
    assert tools["cancel"].parameters["required"] == ["cmd_hash"]
    assert tools["health"].parameters.get("required", []) == []
    assert tools["read"].parameters.get("required", []) == []
    start = tools["agent_start"].parameters["properties"]
    assert start["task_summary"]["maxLength"] == 120
    assert start["intent"]["maxLength"] == 160
    assert start["work_scope"]["maxItems"] == 4
    assert start["work_scope"]["items"]["maxLength"] == 80


@pytest.mark.asyncio
async def test_mcp_start_run_and_recovery_structured_results():
    mcp = build_mcp(FakeService())
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    started = await tools["agent_start"].run(
        {
            "task_summary": "Implement registry",
            "intent": "Inspect schema",
            "work_scope": ["repo:storage"],
        },
        convert_result=True,
    )
    assert isinstance(started, CallToolResult)
    assert started.structuredContent["self"]["agent_id"] == "Kilo-7K2M"

    run = await tools["run"].run({"agent_id": "Kilo-7K2M", "cmd": "printf ok"}, convert_result=True)
    assert run.content[0].text == "Command 1234abcd queued."
    assert run.structuredContent["agent_id"] == "Kilo-7K2M"

    recovery = await tools["recovery"].run(
        {"cmd": "printf recovery-ready", "agent_id": "Kilo-7K2M"}, convert_result=True
    )
    assert recovery.structuredContent["cmd_hash"] == "abcd1234"
    assert recovery.structuredContent["displayed_lines_count"] == 1

@pytest.mark.asyncio
async def test_mcp_health_and_emergency_tools_do_not_require_agent_id():
    mcp = build_mcp(FakeService())
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    health = await tools["health"].run({}, convert_result=True)
    assert health.structuredContent["ok"] is True
    assert health.structuredContent["agent_id"] == "anonymous"
    recovery = await tools["recovery"].run({"cmd": "printf recovery-ready"}, convert_result=True)
    assert recovery.structuredContent["agent_id"] == "anonymous"
    cancelled = await tools["cancel"].run({"cmd_hash": "1234abcd"}, convert_result=True)
    assert cancelled.structuredContent["agent_id"] == "anonymous"

@pytest.mark.asyncio
async def test_mcp_read_requires_both_agent_id_and_cmd_hash_or_neither():
    mcp = build_mcp(FakeService())
    read = {tool.name: tool for tool in mcp._tool_manager.list_tools()}["read"]
    global_read = await read.run({}, convert_result=True)
    assert global_read.structuredContent["ok"] is True
    assert global_read.structuredContent["agent_id"] == "anonymous"
    scoped = await read.run(
        {"agent_id": "Kilo-7K2M", "cmd_hash": "1234abcd"},
        convert_result=True,
    )
    assert scoped.structuredContent["ok"] is True
    assert scoped.structuredContent["status"] == "completed"
    one_sided = await read.run({"cmd_hash": "1234abcd"}, convert_result=True)
    assert one_sided.structuredContent["ok"] is False
    assert one_sided.structuredContent["error"].startswith("read.scope:")
