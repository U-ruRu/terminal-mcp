"""Independent public-contract oracles defined before the implementation.

No runtime access: adapters receive isolated fake application responses; storage
checks use tmp_path SQLite. Expectations come from the requested wire contract,
not from production projection helpers. Run against an exact candidate checkout.
"""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.mcp import roles
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore

ORACLE = json.loads((Path(__file__).parent / "fixtures/compact_response_oracle.json").read_text())
NOW = "2026-01-01T00:00:00Z"
PUBLIC_NAME = "Alpha-7 acceptance"
OPAQUE = ORACLE["opaque_user_object"]


def _claim():
    return {
        "agent_name": PUBLIC_NAME,
        "claimed_at": NOW,
        "claim_age_seconds": 0,
        "claim_intent": "independent acceptance",
        "role": "owner",
    }


def _task(*, snapshot=False):
    task = {
        "namespace": "acceptance",
        "task_id": "ONE",
        "title": "Wire contract",
        "lane": "review",
        "priority": "P2",
        "state": "in_progress",
        "operational_status": "in_progress",
        "revision": 3,
        "next_action": "review",
        "blocking_dependencies": [],
    }
    if snapshot:
        return {
            **task,
            "claim": _claim(),
            "description_preview": "body",
            "description_truncated": False,
            "latest_checkpoint": None,
        }
    return {
        **task,
        "description": "body",
        "cooperative": False,
        "checkpoint": deepcopy(OPAQUE),
        "result": deepcopy(OPAQUE),
        "tags": ["acceptance"],
        "isolation_hint": "isolated",
        "input_refs": [],
        "output_refs": [],
        "participants": [],
        "resource_context": {},
        "relations": [],
        "dependencies": [],
        "claims": [_claim()],
        "owner": _claim(),
        "active": True,
        "output_state_id": 1,
        "review_requirements": ["A"],
        "state_changed_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _run(status="queued"):
    return {
        "ok": True,
        "cmd_hash": "aabbccdd",
        "status": status,
        "queue_id": 3,
        "exit_code": 0 if status == "completed" else None,
        "execution_started": status != "queued",
        "queue_position": 0,
        "claimed_at": NOW,
        "started_at": NOW,
        "finished_at": NOW,
        "task_scope": "none",
        "task_targets": [],
        "task_scope_options": ["none"],
        "lines": ["must remain available via read"],
        "overall_lines_count": 1,
        "displayed_lines_count": 1,
        "has_more": False,
        "output_truncated": False,
        "output_retained": True,
        "output_bytes": 4,
        "logical_agent_id": "internal-agent",
        "work_session_id": "internal-session",
        "session_epoch": 1,
        "session_lifecycle": {"state": "active", "remaining_seconds": 0, "hard_expires_at": NOW},
        "messages": [],
        "pending_messages": [],
        "ack_required_pending": False,
        "alert_pending": False,
    }


def _message(mode="notify", *, text="incoming", msg_id="acceptance:msg:one"):
    return {
        "message_hash": msg_id,
        "message_id": msg_id,
        "sender": PUBLIC_NAME,
        "text": text,
        "mode": mode,
        "state": "seen",
        "timestamp": NOW,
        "acknowledged": False,
        "replied": False,
        "truncated": False,
    }


def _server(monkeypatch, role, **responses):
    async def touch_provider(*_args, **_kwargs):
        return None

    app = SimpleNamespace(
        service=SimpleNamespace(persistent=None),
        session_gate=SimpleNamespace(touch_provider=touch_provider),
    )
    for operation, response in responses.items():

        async def invoke(*_args, _response=response, **_kwargs):
            return deepcopy(_response)

        setattr(app, operation, invoke)
    monkeypatch.setattr(roles, "get_application", lambda *_args, **_kwargs: app)
    monkeypatch.setattr(roles, "_actor", lambda *_args, **_kwargs: ActorContext(endpoint_role=role))

    async def identity(*_args, **_kwargs):
        return {
            "status": "resolved",
            "public_name": PUBLIC_NAME,
            "logical_agent_id": "internal-agent",
            "provider": "test",
        }

    monkeypatch.setattr(roles, "_health_identity", identity)
    return roles.build_role_mcp(app.service, role)


async def _call(server, name, arguments):
    tool = server._tool_manager.get_tool(name)
    response = await tool.run(arguments, convert_result=True)
    assert response.isError is False, response
    data = response.structuredContent
    assert json.loads(response.content[0].text) == data
    Draft202012Validator(tool.output_schema).validate(data)
    tool.fn_metadata.output_model.model_validate(data)
    return data, response


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running", "completed", "failed", "cancelled"])
async def test_run_exact_keys_and_actual_status(monkeypatch, status):
    raw = _run(status)
    before = deepcopy(raw)
    data, _ = await _call(
        _server(monkeypatch, "executor", cmd=raw), "command_run", {"command": "printf fixture"}
    )
    assert data == {
        "ok": True,
        "remaining_seconds": 0,
        "cmd_hash": "aabbccdd",
        "status": status,
        "queue_id": 3,
    }
    assert raw == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,requirement", [("notify", None), ("ack", "ack"), ("alert", "reply")])
@pytest.mark.parametrize("pending_only", [False, True])
async def test_incoming_union_is_root_only_and_actionable(
    monkeypatch, mode, requirement, pending_only
):
    raw = _run()
    msg = _message(mode)
    raw["messages"] = [] if pending_only else [deepcopy(msg)]
    raw["pending_messages"] = [deepcopy(msg)]
    before = deepcopy(raw)
    data, _ = await _call(
        _server(monkeypatch, "executor", cmd=raw), "command_run", {"command": "printf fixture"}
    )
    expected = {"id": msg["message_hash"], "sender": PUBLIC_NAME, "text": "incoming"}
    if requirement:
        expected["requirements"] = requirement
    assert data.get("messages") == [expected], data
    assert "coordination" not in data and "pending_messages" not in data
    assert raw == before, "serialization mutated or acknowledged a message"


@pytest.mark.asyncio
async def test_claim_ordinary_success_is_exactly_ok(monkeypatch):
    raw = {
        "ok": True,
        "action": "claim",
        "task": _task(snapshot=True),
        "warnings": [],
        "messages": [],
        "pending_messages": [],
    }
    data, _ = await _call(
        _server(monkeypatch, "executor", task=raw),
        "task_claim",
        {
            "action": "claim",
            "namespace": "acceptance",
            "task_id": "ONE",
            "claim_intent": "review",
            "expected_revision": 3,
        },
    )
    assert data == {"ok": True}


@pytest.mark.asyncio
async def test_claim_keeps_nonempty_required_messages_and_warnings(monkeypatch):
    warning = {"code": "review_required", "severity": "warning", "message": "Read review"}
    msg = _message("alert")
    raw = {
        "ok": True,
        "action": "claim",
        "task": _task(snapshot=True),
        "warnings": [warning],
        "messages": [msg],
        "pending_messages": [deepcopy(msg)],
    }
    data, _ = await _call(
        _server(monkeypatch, "executor", task=raw),
        "task_claim",
        {"action": "claim", "namespace": "acceptance", "task_id": "ONE", "claim_intent": "review"},
    )
    assert data["ok"] is True
    assert data.get("messages") == [
        {
            "id": msg["message_hash"],
            "sender": PUBLIC_NAME,
            "text": "incoming",
            "requirements": "reply",
        }
    ]
    assert any(item["code"] == warning["code"] for item in data.get("warnings", []))
    assert "task" not in data and "action" not in data and "session_lifecycle" not in data


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["command_run", "task_claim"])
async def test_compaction_preserves_intent_warning_metadata(monkeypatch, operation):
    raw = (
        _run()
        if operation == "command_run"
        else {"ok": True, "action": "claim", "task": _task(snapshot=True), "warnings": []}
    )
    warning = "Intent is stale; next write will be blocked until refreshed."
    raw["_meta"] = {"intent_warning": warning}
    service_method = "cmd" if operation == "command_run" else "task"
    arguments = (
        {"command": "printf fixture"}
        if operation == "command_run"
        else {
            "action": "claim",
            "namespace": "acceptance",
            "task_id": "ONE",
            "claim_intent": "review",
        }
    )
    data, response = await _call(
        _server(monkeypatch, "executor", **{service_method: raw}), operation, arguments
    )
    assert data["ok"] is True
    assert (response.meta or {}).get("intent_warning") == warning


@pytest.mark.asyncio
async def test_detail_field_removal_and_opaque_values(monkeypatch):
    raw = {"ok": True, "task": _task()}
    before = deepcopy(raw)
    data, _ = await _call(
        _server(monkeypatch, "coordinator", observe=raw),
        "task_get",
        {"namespace": "acceptance", "task_id": "ONE", "detail": "detail"},
    )
    assert data["ok"] is True, data
    assert "detail" not in data
    task = data["task"]
    assert not set(ORACLE["removed_detail"]) & task.keys()
    assert not set(ORACLE["nonempty_detail"]) & task.keys()
    assert task["checkpoint"] == OPAQUE and task["result"] == OPAQUE
    assert task["cooperative"] is False
    assert task["claims"] == [
        {"agent_name": PUBLIC_NAME, "claimed_at": NOW, "claim_intent": "independent acceptance"}
    ]
    assert raw == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("input_refs", ["input"]),
        ("output_refs", ["output"]),
        ("resource_context", {"repo": "fixture", "path": "src", "scope": "review"}),
        ("participants", [{**_claim(), "role": "participant"}]),
    ],
)
async def test_detail_keeps_nonempty_service_fields(monkeypatch, field, value):
    raw = {"ok": True, "task": _task()}
    raw["task"][field] = deepcopy(value)
    data, _ = await _call(
        _server(monkeypatch, "coordinator", observe=raw),
        "task_get",
        {"namespace": "acceptance", "task_id": "ONE", "detail": "detail"},
    )
    assert data["ok"] is True, data
    assert data["task"][field]
    if field != "participants":
        assert data["task"][field] == value


@pytest.mark.asyncio
async def test_detail_partitions_blocking_relations_without_mutation(monkeypatch):
    raw = {"ok": True, "task": _task()}
    blocker = {
        "namespace": "acceptance",
        "task_id": "BLOCKER",
        "state": "ready",
        "archived": False,
        "satisfied": False,
    }
    relation = {
        "direction": "outgoing",
        "kind": "depends_on",
        "namespace": "acceptance",
        "task_id": "BLOCKER",
        "created_at": NOW,
        "created_by": PUBLIC_NAME,
    }
    other = {**relation, "kind": "related_to", "task_id": "OTHER"}
    raw["task"].update(
        blocking_dependencies=[blocker], dependencies=[blocker], relations=[relation, other]
    )
    before = deepcopy(raw)
    data, _ = await _call(
        _server(monkeypatch, "coordinator", observe=raw),
        "task_get",
        {"namespace": "acceptance", "task_id": "ONE", "detail": "detail"},
    )
    assert data["ok"] is True, data
    task = data["task"]
    assert task["blocking_dependencies"] == [blocker]
    assert task["relations"] == [other]
    assert "dependencies" not in task
    assert raw == before


@pytest.mark.asyncio
async def test_snapshot_removes_service_fields_keeps_false_and_claim(monkeypatch):
    raw = {"ok": True, "task": _task(snapshot=True)}
    data, _ = await _call(
        _server(monkeypatch, "coordinator", observe=raw),
        "task_get",
        {"namespace": "acceptance", "task_id": "ONE", "detail": "snapshot"},
    )
    assert data["ok"] is True, data
    assert "detail" not in data
    task = data["task"]
    assert "operational_status" not in task and "blocking_dependencies" not in task
    assert task["description_truncated"] is False
    assert task["claim"] == {
        "agent_name": PUBLIC_NAME,
        "claimed_at": NOW,
        "claim_intent": "independent acceptance",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy", [True, False])
@pytest.mark.parametrize("extended", [True, False])
async def test_health_exact_fields_and_full_diagnostics(monkeypatch, healthy, extended):
    raw = {
        "ok": healthy,
        "status": "healthy" if healthy else "degraded",
        "application": "terminal-mcp",
        "version": "test",
        "storage": "sqlite",
        "agent_name": PUBLIC_NAME,
        "auth_mode": "test",
        "components": [
            {"id": "storage", "status": "healthy"},
            {"id": "worker", "status": "healthy" if healthy else "failed"},
        ],
        "terminal": {
            "ok": True,
            "scheduler": "fifo",
            "parallelism": 1,
            "queue_size": 0,
            "degraded": False,
            "output_cache": {"retained_bytes": 0},
        },
        "workflow": {
            "ok": True,
            "active_claims": 0,
            "unreleased_claims": 0,
            "stale_claims": 0,
            "by_state": {"ready": 0},
            "live_claims": 0,
        },
    }
    data, _ = await _call(
        _server(monkeypatch, "coordinator", health=raw), "health", {"extended": extended}
    )
    assert data["ok"] is True, data
    expected = set(ORACLE["compact_health_keys"])
    if extended:
        expected |= set(ORACLE["extended_health_extra"])
    assert set(data) == expected
    assert data["healthy"] is healthy and data["agent_name"] == PUBLIC_NAME
    if extended:
        for field in ORACLE["extended_health_extra"]:
            assert data[field] == raw[field]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["events", "comments"])
async def test_history_items_and_cursor_survive_projection(monkeypatch, kind):
    raw = {"ok": True, "task": _task()}
    rows = [
        {
            "id": i,
            "event_type": "persistent_mutation" if kind == "events" else "comment",
            "payload": deepcopy(OPAQUE),
            "created_at": NOW,
            "agent_name": PUBLIC_NAME,
            "logical_agent_id": "internal-agent",
            "work_session_id": "internal-session",
            "session_epoch": 1,
        }
        for i in range(5, 0, -1)
    ]
    raw["task"][kind] = rows
    server = _server(monkeypatch, "coordinator", observe=raw)
    cursor = None
    got = []
    while True:
        arguments = {
            "namespace": "acceptance",
            "task_id": "ONE",
            "detail": "history",
            "history_kind": kind,
            "limit": 2,
        }
        if cursor:
            arguments["cursor"] = cursor
        data, _ = await _call(server, "task_get", arguments)
        assert data["ok"] is True, data
        assert "detail" not in data and "kind" not in data["task"]
        got.extend(data["task"]["items"])
        cursor = data["task"].get("next_cursor")
        if not cursor:
            break
        assert len(got) <= 5
    assert [item["id"] for item in got] == [5, 4, 3, 2, 1]
    for expected, actual in zip(rows, got, strict=True):
        assert actual["payload"] == OPAQUE
        if kind == "events":
            assert actual == {k: v for k, v in expected.items() if k not in ORACLE["removed_event"]}
        else:
            assert actual == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    ORACLE["legacy_checkpoint_values"]
    + [
        {"text": "keep whole object", "metadata": {"flag": False, "zero": 0, "null": None}},
        {},
        "",
    ],
)
async def test_stored_checkpoint_snapshot_preserves_original_type(monkeypatch, tmp_path, value):
    db = tmp_path / "checkpoint.sqlite3"
    await SqliteRepository(db, tmp_path / "output.sqlite3").initialize()
    store = TaskStore(db)
    coordinator = TaskCoordinator(store)
    created = await coordinator.mutate(
        PUBLIC_NAME,
        action="create",
        namespace="acceptance",
        task_id="ONE",
        title="Checkpoint type",
        isolation_hint="none",
    )
    assert created["ok"] is True, created
    updated = await coordinator.mutate(
        PUBLIC_NAME,
        action="checkpoint",
        namespace="acceptance",
        task_id="ONE",
        checkpoint=deepcopy(value),
    )
    assert updated["ok"] is True, updated
    stored_before = await store.get_task("acceptance", "ONE")
    events_before = await store.list_events("acceptance", "ONE")
    assert stored_before["checkpoint"] == value
    result = await coordinator.list(namespace="acceptance", task_id="ONE", snapshot=True)
    data, _ = await _call(
        _server(monkeypatch, "coordinator", observe=result),
        "task_get",
        {"namespace": "acceptance", "task_id": "ONE", "detail": "snapshot"},
    )
    assert data["ok"] is True, data
    checkpoint = data["task"].get("latest_checkpoint")
    assert checkpoint is not None, "an explicit empty checkpoint is still a recorded checkpoint"
    assert checkpoint.get("checkpoint") == value
    assert type(checkpoint["checkpoint"]) is type(value)
    assert "text" not in checkpoint and "revision" not in checkpoint
    assert await store.get_task("acceptance", "ONE") == stored_before
    assert await store.list_events("acceptance", "ONE") == events_before
