import json

import pytest

from terminal_mcp.mcp.server import build_mcp


class GateBackend:
    def __init__(self):
        self.state = {
            "messages": [],
            "pending_messages": [],
            "ack_required_pending": False,
            "alert_pending": False,
        }
        self.calls = []

    async def access_identity(self, code):
        assert code == "0042"
        return {
            "ok": True,
            "logical_agent_id": "la_agent",
            "work_session_id": "ws_agent",
            "session_ref": "ws_agent",
            "session_epoch": 3,
            "public_name": "Alpha",
        }

    async def message_state(self, **kwargs):
        self.calls.append(("message_state", kwargs))
        return dict(self.state)

    async def run(self, command, **kwargs):
        self.calls.append(("run", command, kwargs))
        return {"ok": True, "cmd_hash": "runhash", "error": None}

    async def cancel(self, cmd_hash, **kwargs):
        self.calls.append(("cancel", cmd_hash, kwargs))
        return {"ok": True, "cmd_hash": cmd_hash, "error": None}

    async def recovery(self, command, **kwargs):
        self.calls.append(("recovery", command, kwargs))
        return {"ok": True, "cmd_hash": "recoveryhash", "lines": [], "error": None}


class GateService:
    def __init__(self):
        self.persistent = GateBackend()

    async def read(self, cmd_hash=None, lines_count=500, offset=None, agent_id=None):
        return {
            "ok": True,
            "cmd_hash": cmd_hash,
            "lines": [],
            "next_offset": 0,
            "displayed_lines_count": 0,
            "error": None,
        }


def body(result):
    content = result.content if hasattr(result, "content") else result
    return json.loads(content[0].text)


@pytest.mark.asyncio
async def test_ack_gate_blocks_only_run_and_surfaces_messages_on_identity_read():
    service = GateService()
    service.persistent.state = {
        "messages": [{"message_hash": "ack-1", "mode": "ack", "text": "ack me"}],
        "pending_messages": [{"message_hash": "ack-1", "mode": "ack", "text": "ack me"}],
        "ack_required_pending": True,
        "alert_pending": False,
    }
    cmd = {tool.name: tool for tool in build_mcp(service)._tool_manager.list_tools()}["cmd"]

    blocked = body(
        await cmd.run(
            {
                "request": {
                    "action": "run",
                    "code": "0042",
                    "command": "printf blocked",
                }
            },
            convert_result=True,
        )
    )
    assert blocked["code"] == "coordination_ack_required"
    assert blocked["details"]["pending_messages"][0]["message_hash"] == "ack-1"

    read = body(
        await cmd.run(
            {"request": {"action": "read", "code": "0042", "cmd_hash": "abcd1234"}},
            convert_result=True,
        )
    )
    assert read["ok"] is True
    assert read["messages"][0]["mode"] == "ack"

    recovery = body(
        await cmd.run(
            {
                "request": {
                    "action": "recovery",
                    "code": "0042",
                    "command": "printf allowed",
                }
            },
            convert_result=True,
        )
    )
    assert recovery["ok"] is True


@pytest.mark.asyncio
async def test_alert_gate_blocks_work_but_never_blocks_cancel():
    service = GateService()
    service.persistent.state = {
        "messages": [{"message_hash": "alert-1", "mode": "alert", "text": "reply"}],
        "pending_messages": [{"message_hash": "alert-1", "mode": "alert", "text": "reply"}],
        "ack_required_pending": True,
        "alert_pending": True,
    }
    cmd = {tool.name: tool for tool in build_mcp(service)._tool_manager.list_tools()}["cmd"]

    for request in (
        {"action": "run", "code": "0042", "command": "printf blocked"},
        {"action": "read", "code": "0042", "cmd_hash": "abcd1234"},
        {"action": "recovery", "code": "0042", "command": "printf blocked"},
    ):
        blocked = body(await cmd.run({"request": request}, convert_result=True))
        assert blocked["code"] == "coordination_alert"
        assert blocked["details"]["pending_messages"][0]["message_hash"] == "alert-1"

    cancelled = body(
        await cmd.run(
            {"request": {"action": "cancel", "code": "0042", "cmd_hash": "abcd1234"}},
            convert_result=True,
        )
    )
    assert cancelled["ok"] is True
    assert cancelled["messages"][0]["mode"] == "alert"
