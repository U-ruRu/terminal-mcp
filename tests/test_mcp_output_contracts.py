import json

import pytest
from jsonschema import Draft202012Validator

from terminal_mcp.mcp.output_contracts import (
    CmdOutput,
    ContextOutput,
    HealthOutput,
    MessageOutput,
    ObserveOutput,
    OutputContractViolation,
    SessionOutput,
    TaskOutput,
    cmd_result,
    context_result,
    health_result,
    message_result,
    observe_result,
    session_result,
    task_result,
)
from terminal_mcp.mcp.server import build_mcp
class FakeService:
    pass


def validate(model, result):
    Draft202012Validator(model.model_json_schema()).validate(result.structuredContent)
    assert json.loads(result.content[0].text)["ok"] is True


TASK = {
    "namespace": "project", "task_id": "T-1", "title": "Task",
    "lane": "implementation", "priority": "P1", "state": "in_progress",
    "operational_status": "in_progress", "revision": 4, "isolation_hint": "task/T-1",
}


def test_discovery_has_closed_success_output_schema_for_all_public_tools():
    tools = {tool.name: tool for tool in build_mcp(FakeService())._tool_manager.list_tools()}
    assert set(tools) == {"session", "observe", "message", "task", "cmd", "context", "health"}
    for tool in tools.values():
        schema = tool.fn_metadata.output_schema
        assert schema
        assert '"ok"' in json.dumps(schema)
        assert '"additionalProperties": false' in json.dumps(schema)


@pytest.mark.parametrize(
    ("action", "raw"),
    [
        ("start", {"ok": True, "mode": "persistent", "public_name": "Alpha", "session_ref": "ws1", "session_epoch": 1, "hard_expires_at": "x"}),
        ("start", {"ok": True, "mode": "legacy", "public_name": "Alpha", "session_ref": "ws1", "session_epoch": 1, "hard_expires_at": "x", "access_code": "1234"}),
        ("end", {"ok": True, "mode": "persistent", "public_name": "Alpha", "session_ref": "ws1", "stopping": False}),
        ("interrupt", {"ok": True, "mode": "persistent", "public_name": "Alpha", "session_ref": "ws1", "stopping": False}),
    ],
)
def test_session_variants(action, raw):
    validate(SessionOutput, session_result(raw, action))


@pytest.mark.parametrize("subject,raw", [
    ("sessions", {"ok": True, "sessions": [{"public_name": "Alpha", "mode": "persistent", "authority_node_id": "main", "access_generation": 1}]}),
    ("tasks", {"ok": True, "summary": {"visible": 1, "returned": 1}, "tasks": [TASK], "tag_counts": {}, "namespaces": ["project"], "next_cursor": None}),
])
def test_observe_variants(subject, raw):
    validate(ObserveOutput, observe_result(raw, subject))


@pytest.mark.parametrize("kwargs,raw", [
    ({"text": None, "message_hash": None, "show_all": False}, {"ok": True, "sender": "Alpha", "messages": []}),
    ({"text": None, "message_hash": None, "show_all": True}, {"ok": True, "sender": "Alpha", "messages": []}),
    ({"text": "hi", "message_hash": None, "show_all": False}, {"ok": True, "sender": "Alpha", "message_hash": "m1", "delivered_to": ["Bravo"], "scope": "direct"}),
    ({"text": None, "message_hash": "m1", "show_all": False}, {"ok": True, "sender": "Alpha", "message_hash": "m1"}),
    ({"text": "reply", "message_hash": "m1", "show_all": False}, {"ok": True, "sender": "Alpha", "message_hash": "m2", "reply_to": "m1"}),
])
def test_message_variants(kwargs, raw):
    validate(MessageOutput, message_result(raw, sender="Alpha", target="Bravo", mode="notify", require_reply=False, alert=False, **kwargs))


@pytest.mark.parametrize("action", ["create","claim","release","update","checkpoint","comment","relate","unrelate","state","done","archive","review"])
def test_task_variants(action):
    validate(TaskOutput, task_result({"ok": True, "task": TASK, "warnings": []}, action))


@pytest.mark.parametrize("action,raw", [
    ("run", {"ok": True, "cmd_hash": "c1", "status": "queued", "queue_id": 1}),
    ("run", {"ok": True, "cmd_hash": "c1", "status": "running", "queue_id": 1, "execution_started": True}),
    ("read", {"ok": True, "cmd_hash": "c1", "status": "running", "exit_code": None, "lines": [], "next_offset": 0}),
    ("read", {"ok": True, "cmd_hash": "c1", "status": "completed", "exit_code": 0, "lines": ["ok"], "next_offset": 1, "next_cursor": None, "has_more": False}),
    ("read", {"ok": True, "cmd_hash": "c1", "status": "failed", "exit_code": 2, "lines": ["bad"], "next_offset": 1}),
    ("cancel", {"ok": True, "cmd_hash": "c1", "status": "cancelled"}),
    ("recovery", {"ok": True, "cmd_hash": "c1", "status": "running", "exit_code": None, "lines": []}),
])
def test_cmd_variants(action, raw):
    validate(CmdOutput, cmd_result(raw, action))


@pytest.mark.parametrize("action,raw", [
    ("list", {"ok": True, "primary": [], "additional": []}),
    ("create", {"ok": True, "entry": {"id": 1, "summary": "s", "content": "c", "primary": False}}),
    ("update", {"ok": True, "entry": {"id": 1, "summary": "s", "content": "c", "primary": True}}),
    ("delete", {"ok": True, "deleted_id": 1}),
])
def test_context_variants(action, raw):
    validate(ContextOutput, context_result(raw, action))


@pytest.mark.parametrize("degraded", [False, True])
def test_health_variants(degraded):
    raw = {
        "ok": True, "application": "terminal-mcp", "version": "0.11.2", "storage": "ok", "auth_mode": "oauth",
        "terminal": {"ok": not degraded, "user": "root", "uid": 0, "gid": 0, "cwd": "/", "privilege": "root", "shell": "/bin/bash", "terminal_user": "root", "scheduler": "numbered-fifo", "parallelism": 4, "queue_size": 0, "running_commands": [], "degraded": degraded},
        "workflow": {"ok": not degraded, "by_state": {}, "by_lane": {}, "reviews": {}},
    }
    validate(HealthOutput, health_result(raw))


@pytest.mark.parametrize("bad", [
    {"ok": True, "status": "queued"},
    {"ok": True, "cmd_hash": "c1", "status": "bogus"},
    {"ok": True, "cmd_hash": 42, "status": "queued"},
])
def test_contract_violation_rejects_bad_implementation_result(bad):
    with pytest.raises(OutputContractViolation, match="output_contract_violation tool=cmd"):
        cmd_result(bad, "run")


def test_unknown_backend_field_does_not_expand_public_contract():
    result = session_result({"ok": True, "mode": "persistent", "public_name": "Alpha", "session_ref": "ws", "unexpected": "secret"}, "start")
    assert "unexpected" not in result.structuredContent
