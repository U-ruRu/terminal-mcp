import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

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
    _result,
)
from terminal_mcp.mcp.server import build_mcp
class FakeService:
    pass


def validate(model, result):
    Draft202012Validator(model.success_schema()).validate(result.structuredContent)
    assert json.loads(result.content[0].text)["ok"] is True


TASK = {
    "namespace": "project", "task_id": "T-1", "title": "Task",
    "lane": "implementation", "priority": "P1", "state": "in_progress",
    "operational_status": "in_progress", "revision": 4, "isolation_hint": "task/T-1",
}
TASK_LIST_ITEM = {
    "namespace": "project", "task_id": "T-1", "title": "Task",
    "lane": "implementation", "priority": "P1", "state": "in_progress",
    "operational_status": "in_progress", "revision": 4, "claimed_by": None,
    "blocking_count": 0, "has_checkpoint": False,
}
TASK_SNAPSHOT = {
    "namespace": "project", "task_id": "T-1", "title": "Task",
    "lane": "implementation", "priority": "P1", "state": "in_progress",
    "operational_status": "in_progress", "revision": 4, "claim": None,
    "next_action": "continue", "description_preview": "bounded",
    "description_truncated": False, "latest_checkpoint": None,
    "blocking_dependencies": [],
}


def test_discovery_has_closed_success_output_schema_for_all_public_tools():
    tools = {tool.name: tool for tool in build_mcp(FakeService())._tool_manager.list_tools()}
    assert set(tools) == {"session", "observe", "message", "task", "cmd", "context", "health"}
    for tool in tools.values():
        schema = tool.fn_metadata.output_schema
        assert schema
        assert schema["type"] == "object"
        assert '"ok"' in json.dumps(schema)
        assert '"additionalProperties": false' in json.dumps(schema)
        assert not _has_unbounded_object(schema), tool.name
        Draft202012Validator.check_schema(schema)
    assert "$defs" in tools["session"].fn_metadata.output_schema
    assert "$defs" in tools["cmd"].fn_metadata.output_schema
    assert "$defs" in tools["task"].fn_metadata.output_schema


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
    ("tasks", {"ok": True, "summary": {"visible": 1, "returned": 1}, "tasks": [TASK_LIST_ITEM], "tag_counts": {}, "namespaces": ["project"], "next_cursor": None}),
    ("namespaces", {"ok": True, "namespaces": ["project"], "next_cursor": None}),
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
    task = TASK_SNAPSHOT if action == "claim" else TASK
    validate(TaskOutput, task_result({"ok": True, "task": task, "warnings": []}, action))


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


def test_contract_violation_rejects_undeclared_structured_property():
    with pytest.raises(OutputContractViolation, match="output_contract_violation tool=cmd"):
        _result(
            "cmd",
            "run",
            CmdOutput,
            {"ok": True},
            {
                "ok": True,
                "action": "run",
                "command": {"cmd_hash": "c1", "status": "queued"},
                "undeclared_backend_field": "must-not-publish",
            },
        )


def _has_unbounded_object(node):
    if isinstance(node, dict):
        if node.get("additionalProperties") is True:
            return True
        return any(_has_unbounded_object(value) for value in node.values())
    if isinstance(node, list):
        return any(_has_unbounded_object(value) for value in node)
    return False


def test_message_compact_summary_preserves_bounded_contract_fields():
    from terminal_mcp.core.read_contract import summary_message

    compact = summary_message(
        {
            "message_hash": "msg-summary-1",
            "sender": "Alpha",
            "target": "Bravo",
            "mode": "ack",
            "created_at": "2026-10-05T10:00:00.000Z",
            "state": "read",
            "read_at": "2026-10-05T10:00:01.000Z",
            "replied_at": "2026-10-05T10:00:02.000Z",
            "text": "x" * 4000,
            "namespace": "terminal-mcp",
            "task_id": "T-1",
        }
    )
    result = message_result(
        {
            "ok": True,
            "sender": "Bravo",
            "messages": [compact],
            "next_cursor": None,
        },
        sender="Bravo",
        target=None,
        mode=None,
        require_reply=False,
        alert=False,
        text=None,
        message_hash=None,
        show_all=False,
    )
    validate(MessageOutput, result)
    message = result.structuredContent["messages"][0]
    assert message["message_id"] == compact["message_id"] == "msg-summary-1"
    assert message["message_hash"] == "msg-summary-1"
    assert message["timestamp"] == compact["timestamp"]
    assert message["acknowledged"] is True
    assert message["replied"] is True
    assert message["truncated"] is True
    assert message["text"] == compact["text"]


def test_task_nested_backend_fields_are_projected_out_and_schema_is_closed():
    raw_task = {
        **TASK,
        "checkpoint": {"step": 2, "backend_only_new_field": "inside-explicit-payload"},
        "result": {"summary": "done", "backend_only_new_field": "inside-explicit-payload"},
        "resource_context": {
            "repo": "terminal-mcp",
            "path": "src",
            "scope": "review",
            "backend_only_new_field": "must-not-publish",
        },
        "blocking_dependencies": [
            {
                "namespace": "project",
                "task_id": "D-1",
                "state": "ready",
                "archived": False,
                "satisfied": False,
                "backend_only_new_field": "must-not-publish",
            }
        ],
        "dependencies": [
            {
                "namespace": "project",
                "task_id": "D-2",
                "state": "done",
                "archived": False,
                "satisfied": True,
                "backend_only_new_field": "must-not-publish",
            }
        ],
        "relations": [
            {
                "direction": "outgoing",
                "kind": "review_of",
                "namespace": "project",
                "task_id": "R-1",
                "created_at": "2026-10-05T10:00:00.000Z",
                "created_by": "logical-1",
                "backend_only_new_field": "must-not-publish",
            }
        ],
        "output_states": [
            {
                "output_state_id": 7,
                "output_refs": ["sha"],
                "created_at": "2026-10-05T10:00:00.000Z",
                "backend_only_new_field": "must-not-publish",
            }
        ],
        "events": [
            {
                "id": 10,
                "event_type": "checkpoint",
                "payload": {"step": 2},
                "created_at": "2026-10-05T10:00:00.000Z",
                "agent_name": "Alpha",
                "backend_only_new_field": "must-not-publish",
            }
        ],
        "comments": [
            {
                "id": 11,
                "event_type": "comment",
                "payload": {"text": "note"},
                "created_at": "2026-10-05T10:00:01.000Z",
                "agent_name": "Alpha",
                "backend_only_new_field": "must-not-publish",
            }
        ],
        "reviews": [
            {
                "output_state_id": 7,
                "output_refs": ["sha"],
                "dimension": "C",
                "verdict": "NON_BLOCKING",
                "evidence": {"tests": 1},
                "warnings": [],
                "reviewed_at": "2026-10-05T10:00:02.000Z",
                "reviewer": "Bravo",
                "agent_name": "Bravo",
                "backend_only_new_field": "must-not-publish",
            }
        ],
    }
    result = task_result({"ok": True, "task": raw_task, "warnings": []}, "update")
    validate(TaskOutput, result)
    structured_task = result.structuredContent["task"]

    assert "backend_only_new_field" not in structured_task["resource_context"]
    for key in (
        "blocking_dependencies",
        "dependencies",
        "relations",
        "output_states",
        "events",
        "comments",
        "reviews",
    ):
        assert "backend_only_new_field" not in structured_task[key][0]

    # Arbitrary domain payloads remain explicit and schema-stable JSON payloads.
    assert json.loads(structured_task["checkpoint"]["serialized"])["backend_only_new_field"] == "inside-explicit-payload"
    assert json.loads(structured_task["result"]["serialized"])["backend_only_new_field"] == "inside-explicit-payload"

    schema = TaskOutput.success_schema()
    assert not _has_unbounded_object(schema)
    Draft202012Validator.check_schema(schema)


def test_health_review_counts_are_closed_and_reject_nested_backend_payload():
    valid = {
        "ok": True,
        "application": "terminal-mcp",
        "version": "0.11.2",
        "storage": "ok",
        "auth_mode": "oauth",
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
        },
        "workflow": {
            "ok": True,
            "by_state": {},
            "by_lane": {},
            "reviews": {"BLOCKING": 2, "NON_BLOCKING": 5},
        },
    }
    result = health_result(valid)
    validate(HealthOutput, result)
    assert result.structuredContent["workflow"]["reviews"] == {
        "BLOCKING": 2,
        "NON_BLOCKING": 5,
    }

    leaked = {
        **valid,
        "workflow": {
            **valid["workflow"],
            "reviews": {"BLOCKING": {"backend_only_new_field": "LEAK"}},
        },
    }
    with pytest.raises(ValidationError):
        health_result(leaked)

    schema = HealthOutput.success_schema()
    assert not _has_unbounded_object(schema)
