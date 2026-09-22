import pytest
from mcp.types import CallToolResult

from terminal_mcp.mcp.server import build_mcp


class FakeService:
    async def agent_start(
        self,
        task_summary=None,
        intent=None,
        details=None,
        work_scope=None,
        agent_id=None,
    ):
        return {
            "ok": True,
            "self": {
                "name": "Kilo",
                **({"agent_id": "Kilo-7K2M"} if agent_id is None else {}),
                "ttl_seconds": 300,
                "task_lease_seconds": 180,
                "task_summary": task_summary or "task",
                "intent": intent or "work",
                "work_scope": work_scope or [],
                "details": details or ["Inspect schema", "Patch schema"],
                "current_step": 1,
            },
            "active": [],
            "overlaps": [],
            "additional_active_agents": 0,
            "pending_messages": [],
        }

    async def coordinate(self, agent_id, step=None, intent=None, show_details=False):
        return {
            "ok": True,
            "agent_name": "Kilo",
            "step": step or 1,
            "intent": intent or "work",
            "detail": "Patch schema" if step == 2 else "Inspect schema",
            "other_details": ["India step 1/2 — inspect | 1. inspect | 2. test"]
            if show_details
            else [],
            "active_agents": ["23:00:01 India deadbeef — Inspect tests"],
            "pending_messages": [],
        }

    async def message(
        self, agent_id, text=None, target=None, message_hash=None, require_reply=False, alert=False
    ):
        return {
            "ok": True,
            "agent_name": "Kilo",
            "message_hash": message_hash or "a1b2c3d4",
            "delivered_to": [] if message_hash else [target or "India"],
            "read_by": ["Kilo"] if message_hash else [],
            "pending_messages": [],
        }

    async def agents(
        self, agent_id=None, *, target=None, show_details=False, show_intents=False,
        show_commands=False, command_hash=None, since_minutes=None
    ):
        return {
            "ok": True,
            "self": {
                "name": "Kilo",
                "ttl_seconds": 300,
                "task_lease_seconds": 180,
                "task_summary": "task",
                "intent": "work",
                "work_scope": ["repo:mcp"],
                "details": ["Inspect", "Patch"],
                "current_step": 1,
            },
            "active": [],
            "overlaps": [],
            "additional_active_agents": 0,
            "pending_messages": [],
        }

    async def agent_finish(self, agent_id):
        return {
            "ok": True,
            "agent_name": "Kilo",
            "finished": True,
            "pending_messages": [],
        }

    async def awareness(self, agent_id=None):
        return {
            "agent_name": "Kilo" if agent_id else "anonymous",
            "active_agents": ["23:00:01 India deadbeef — Inspect tests"],
            "pending_messages": [],
        }

    async def run(self, cmd, agent_id=None, queue_id=None):
        return {
            "ok": True,
            "cmd_hash": "1234abcd",
            "error": None,
            "queue_id": queue_id or 1,
            "queue_position": 1,
            "agent_name": "Kilo",
            "active_agents": ["23:00:01 India deadbeef — Inspect tests"],
            "pending_messages": [],
        }

    async def recovery(self, cmd, agent_id=None):
        return {
            "ok": True,
            "cmd_hash": "abcd1234",
            "lines": ["00:00:00 recovery-ready"],
            "overall_lines_count": 1,
            "displayed_lines_count": 1,
            "exit_code": 0,
            "error": None,
            "duration_ms": 1,
            "agent_name": "Kilo" if agent_id else "anonymous",
            "pending_messages": [],
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
            "agent_name": "Kilo" if agent_id else "anonymous",
            "active_agents": ["23:00:01 India deadbeef — Inspect tests"],
            "pending_messages": [],
        }

    async def cancel(self, cmd_hash, agent_id=None):
        return {
            "ok": True,
            "cmd_hash": cmd_hash,
            "error": None,
            "agent_name": "Kilo" if agent_id else "anonymous",
            "pending_messages": [],
        }

    async def health(self, auth_mode, agent_id=None):
        return {
            "ok": True,
            "agent_name": "Kilo" if agent_id else "anonymous",
            "pending_messages": [],
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
                "scheduler": "numbered-fifo",
                "parallelism": 4,
                "queue_size": 0,
                "running_commands": [],
                "queues": [],
                "worker_health": {},
            },
        }

def test_mcp_tools_advertise_agent_protocol_and_structured_schemas():
    mcp = build_mcp(FakeService())
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    assert set(tools) == {
        "agent_start",
        "coordinate",
        "message",
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
    assert tools["run"].parameters["properties"]["queue_id"]["anyOf"][0]["minimum"] == 1
    assert tools["agents"].parameters.get("required", []) == []
    assert tools["recovery"].parameters["required"] == ["cmd"]
    assert tools["cancel"].parameters["required"] == ["cmd_hash"]
    assert tools["health"].parameters.get("required", []) == []
    assert tools["read"].parameters.get("required", []) == []
    start = tools["agent_start"].parameters["properties"]
    assert start["task_summary"]["anyOf"][0]["maxLength"] == 120
    assert start["intent"]["anyOf"][0]["maxLength"] == 160
    assert start["details"]["anyOf"][0]["maxItems"] == 12
    assert start["details"]["anyOf"][0]["items"]["maxLength"] == 160
    assert start["work_scope"]["anyOf"][0]["maxItems"] == 4
    assert tools["coordinate"].parameters["required"] == ["agent_id"]
    coordinate = tools["coordinate"].parameters["properties"]
    assert coordinate["intent"]["anyOf"][0]["maxLength"] == 160
    assert coordinate["step"]["anyOf"][0]["minimum"] == 1
    assert tools["message"].parameters["required"] == ["agent_id"]
    message = tools["message"].parameters["properties"]
    assert message["message_hash"]["anyOf"][0]["maxLength"] == 8
    assert message["require_reply"]["default"] is False
    assert message["alert"]["default"] is False


@pytest.mark.asyncio
async def test_mcp_start_run_and_recovery_structured_results():
    mcp = build_mcp(FakeService())
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    started = await tools["agent_start"].run(
        {
            "task_summary": "Implement registry",
            "intent": "Inspect schema",
            "details": ["Inspect schema", "Patch schema"],
            "work_scope": ["repo:storage"],
        },
        convert_result=True,
    )
    assert isinstance(started, CallToolResult)
    assert started.structuredContent["self"]["agent_id"] == "Kilo-7K2M"

    task_update = await tools["coordinate"].run(
        {"agent_id": "Kilo-7K2M", "step": 2, "intent": "Inspect the next test"},
        convert_result=True,
    )
    assert task_update.structuredContent["intent"] == "Inspect the next test"
    assert task_update.structuredContent["step"] == 2

    sent = await tools["message"].run(
        {"agent_id": "Kilo-7K2M", "text": "Coordinate", "target": "India"},
        convert_result=True,
    )
    assert sent.structuredContent["message_hash"] == "a1b2c3d4"
    assert sent.structuredContent["delivered_to"] == ["India"]

    run = await tools["run"].run({"agent_id": "Kilo-7K2M", "cmd": "printf ok"}, convert_result=True)
    assert run.content[0].text == "Command 1234abcd queued."
    assert run.structuredContent["agent_name"] == "Kilo"
    assert "India deadbeef — Inspect tests" in run.structuredContent["active_agents"][0]
    assert "7K2M" not in str(run.structuredContent)

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
    assert health.structuredContent["agent_name"] == "anonymous"
    recovery = await tools["recovery"].run({"cmd": "printf recovery-ready"}, convert_result=True)
    assert recovery.structuredContent["agent_name"] == "anonymous"
    cancelled = await tools["cancel"].run({"cmd_hash": "1234abcd"}, convert_result=True)
    assert cancelled.structuredContent["agent_name"] == "anonymous"

@pytest.mark.asyncio
async def test_mcp_read_supports_independent_agent_and_command_scope():
    mcp = build_mcp(FakeService())
    read = {tool.name: tool for tool in mcp._tool_manager.list_tools()}["read"]
    global_read = await read.run({}, convert_result=True)
    assert global_read.structuredContent["ok"] is True
    assert global_read.structuredContent["agent_name"] == "anonymous"
    scoped = await read.run(
        {"agent_id": "Kilo-7K2M", "cmd_hash": "1234abcd"},
        convert_result=True,
    )
    assert scoped.structuredContent["ok"] is True
    assert scoped.structuredContent["status"] == "completed"
    assert scoped.structuredContent["agent_name"] == "Kilo"
    assert "India deadbeef — Inspect tests" in scoped.structuredContent["active_agents"][0]
    assert "7K2M" not in str(scoped.structuredContent)
    command_only = await read.run({"cmd_hash": "1234abcd"}, convert_result=True)
    assert command_only.structuredContent["ok"] is True
    assert command_only.structuredContent["status"] == "completed"
    agent_global = await read.run({"agent_id": "Kilo-7K2M"}, convert_result=True)
    assert agent_global.structuredContent["ok"] is True
    assert agent_global.structuredContent["agent_name"] == "Kilo"
