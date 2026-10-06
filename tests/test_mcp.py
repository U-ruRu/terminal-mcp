import json

import pytest

from terminal_mcp.core.public_errors import public_error
from terminal_mcp.mcp.server import build_mcp
from terminal_mcp.version import __version__


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
            "primary_context": [
                {
                    "id": 1,
                    "summary": "Git workflow",
                    "content": "Use the local Git wrapper.",
                    "primary": True,
                }
            ],
            "active": [],
            "overlaps": None,
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
        self,
        agent_id,
        text=None,
        target=None,
        message_hash=None,
        require_reply=False,
        alert=False,
        namespace=None,
        task_id=None,
    ):
        return {
            "ok": True,
            "agent_name": "Kilo",
            "message_hash": message_hash or "a1b2c3d4",
            "namespace": namespace,
            "task_id": task_id,
            "delivered_to": [] if message_hash or namespace else [target or "India"],
            "read_by": ["Kilo"] if message_hash else [],
            "pending_messages": [],
        }

    async def agents(
        self,
        agent_id=None,
        *,
        target=None,
        show_details=False,
        show_intents=False,
        show_commands=False,
        command_hash=None,
        since_minutes=None,
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
            "overlaps": None,
            "additional_active_agents": 0,
            "pending_messages": [],
        }

    async def context(
        self,
        action,
        *,
        context_id=None,
        summary=None,
        content=None,
        primary=None,
        show_details=False,
        limit=None,
        offset=0,
    ):
        if action == "list":
            first = {"id": 1, "summary": "Git workflow"}
            second = {"id": 2, "summary": "Docs"}
            if show_details:
                first["content"] = "Use the local Git wrapper."
                second["content"] = "Read local docs."
            return {"ok": True, "primary": [first], "additional": [second]}
        if action in {"create", "update"}:
            return {
                "ok": True,
                "entry": {
                    "id": context_id or 3,
                    "summary": summary or "Git workflow",
                    "content": content or "Use the local Git wrapper.",
                    "primary": primary if primary is not None else True,
                },
            }
        if action == "delete":
            return {"ok": True, "deleted_id": context_id}
        return {"ok": False, "error": "unsupported"}

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

    async def run(self, cmd, agent_id=None, queue_id=None, task_scope=None):
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
            "version": __version__,
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


def test_mcp_tools_advertise_canonical_access_surface():
    mcp = build_mcp(FakeService())
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    assert set(tools) == {"session", "observe", "message", "task", "cmd", "context", "health"}

    for tool in tools.values():
        # Project policy keeps MCP tools conservatively marked read-only/non-destructive;
        # mutation authorization is enforced by the tool contract and Access authority.
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False
    assert tools["observe"].annotations.idempotentHint is True
    assert tools["health"].annotations.idempotentHint is True
    for name in {"session", "message", "task", "cmd", "context"}:
        assert tools[name].annotations.idempotentHint is False

    session = tools["session"].parameters
    assert session["required"] == ["action"]
    assert session["properties"]["action"]["enum"] == ["start", "end", "interrupt"]
    assert session["properties"]["mode"]["anyOf"][0]["enum"] == ["persistent", "legacy"]
    assert session["properties"]["code"]["anyOf"][0]["minLength"] == 4
    assert session["properties"]["code"]["anyOf"][0]["maxLength"] == 4

    observe = tools["observe"].parameters["properties"]
    assert "code" not in observe
    assert observe["subject"]["enum"] == ["sessions", "tasks", "namespaces"]
    assert "show_details" not in observe
    assert observe["detail"]["enum"] == ["summary", "full"]
    assert observe["limit"]["default"] == 20
    assert observe["limit"]["maximum"] == 100
    assert "cursor" in observe
    assert observe["state"]["anyOf"][0]["enum"] == [
        "ready",
        "in_progress",
        "blocked",
        "deferred",
        "done",
    ]
    assert observe["operational_status"]["anyOf"][0]["enum"] == [
        "ready",
        "in_progress",
        "blocked",
        "deferred",
        "done",
    ]

    message = tools["message"].parameters
    assert message["required"] == ["sender"]
    assert message["properties"]["code"]["anyOf"][0]["minLength"] == 4
    assert message["properties"]["code"]["anyOf"][0]["maxLength"] == 4
    assert "show_all" not in message["properties"]
    assert message["properties"]["detail"]["enum"] == ["summary", "full"]
    assert message["properties"]["limit"]["default"] == 20
    assert message["properties"]["limit"]["maximum"] == 100
    assert "history" in message["properties"]
    assert "cursor" in message["properties"]

    task = tools["task"].parameters
    assert task["required"] == ["request"]
    task_request = task["properties"]["request"]
    assert task_request["discriminator"]["propertyName"] == "action"
    assert set(task_request["discriminator"]["mapping"]) == {
        "create",
        "claim",
        "release",
        "update",
        "checkpoint",
        "comment",
        "relate",
        "unrelate",
        "state",
        "done",
        "archive",
        "review",
    }
    assert len(task_request["oneOf"]) == 12
    assert "payload" not in task["properties"]
    assert task["$defs"]["TaskClaimRequest"]["properties"]["code"]["minLength"] == 4
    assert task["$defs"]["TaskClaimRequest"]["properties"]["code"]["maxLength"] == 4
    assert "WIP is one live managed-task claim per slot" in tools["task"].description

    cmd = tools["cmd"].parameters
    assert cmd["required"] == ["request"]
    mapping = cmd["properties"]["request"]["discriminator"]["mapping"]
    assert set(mapping) == {"run", "read", "cancel", "recovery"}
    read_schema = cmd["$defs"]["CmdReadRequest"]
    assert "code" in read_schema["properties"]
    assert "code" not in read_schema["required"]
    assert read_schema["required"] == ["action", "cmd_hash"]
    assert "lines_count" not in read_schema["properties"]
    assert "offset" not in read_schema["properties"]
    assert read_schema["properties"]["limit"]["default"] == 100
    assert read_schema["properties"]["limit"]["maximum"] == 100
    assert "cursor" in read_schema["properties"]
    for name in ("CmdRunRequest", "CmdCancelRequest", "CmdRecoveryRequest"):
        assert "code" in cmd["$defs"][name]["required"]
        assert cmd["$defs"][name]["properties"]["code"]["minLength"] == 4
        assert cmd["$defs"][name]["properties"]["code"]["maxLength"] == 4

    context = tools["context"].parameters
    assert context["required"] == ["request"]
    list_schema = context["$defs"]["ContextListRequest"]
    assert "code" not in list_schema["properties"]
    assert "show_details" not in list_schema["properties"]
    assert list_schema["properties"]["detail"]["enum"] == ["summary", "full"]
    assert list_schema["properties"]["limit"]["default"] == 20
    assert list_schema["properties"]["limit"]["maximum"] == 100
    assert "cursor" in list_schema["properties"]
    for name in ("ContextCreateRequest", "ContextUpdateRequest", "ContextDeleteRequest"):
        assert "code" in context["$defs"][name]["required"]

    assert tools["health"].parameters.get("required", []) == []
    assert tools["health"].parameters["properties"] == {}


def _text_json(result):
    content = result.content if hasattr(result, "content") else result
    assert len(content) == 1
    return json.loads(content[0].text)


@pytest.mark.asyncio
async def test_mcp_code_free_read_paths_use_canonical_tools():
    mcp = build_mcp(FakeService())
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    health = _text_json(await tools["health"].run({}, convert_result=True))
    assert health["ok"] is True
    assert health["version"] == __version__
    assert health["agent_name"] == "anonymous"

    read = _text_json(
        await tools["cmd"].run(
            {"request": {"action": "read", "cmd_hash": "1234abcd"}},
            convert_result=True,
        )
    )
    assert read["ok"] is True
    assert read["status"] == "completed"
    assert read["cmd_hash"] == "1234abcd"
    assert read["agent_name"] == "anonymous"

    context = _text_json(
        await tools["context"].run(
            {"request": {"action": "list", "detail": "full"}},
            convert_result=True,
        )
    )
    assert context["primary"][0]["content"] == "Use the local Git wrapper."


@pytest.mark.asyncio
async def test_mcp_mutation_paths_fail_closed_without_access_backend():
    mcp = build_mcp(FakeService())
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    session = _text_json(
        await tools["session"].run(
            {"action": "start", "mode": "persistent", "code": "1234"},
            convert_result=True,
        )
    )
    assert session == public_error("policy_incompatible").as_dict()

    task = _text_json(
        await tools["task"].run(
            {
                "request": {
                    "action": "claim",
                    "code": "1234",
                    "namespace": "project",
                    "task_id": "REV-1",
                    "claim_intent": "inspect",
                }
            },
            convert_result=True,
        )
    )
    assert task == public_error("policy_incompatible").as_dict()

    cmd = _text_json(
        await tools["cmd"].run(
            {"request": {"action": "run", "code": "1234", "command": "printf ok"}},
            convert_result=True,
        )
    )
    assert cmd == public_error("policy_incompatible").as_dict()
