"""Independent public-contract oracles defined before the implementation.

No runtime access: adapters receive isolated fake application responses; storage
checks use tmp_path SQLite. Expectations come from the requested wire contract,
not from production projection helpers. Run against an exact candidate checkout.
"""

import json
from contextlib import asynccontextmanager, nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.persistent_backend import PersistentBackend
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


def _server(monkeypatch, role, *, mesh=None, **responses):
    async def touch_provider(*_args, **_kwargs):
        return None

    app = SimpleNamespace(
        service=SimpleNamespace(persistent=None, access_mesh=mesh),
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


class _IntentBoundaryFixture:
    """Supplies intent state at its actual outer role boundary, without sessions."""

    def __init__(self):
        self.calls = 0
        self.store = SimpleNamespace(attached_slot=lambda _key: None)

    def connection_key(self, _actor):
        return "isolated-binding"

    async def observe_intent_call(self, _actor, *, require_intent):
        assert require_intent is True
        self.calls += 1
        return {
            "state": "stale",
            "warn": self.calls == 1,
            "blocked": self.calls > 1,
            "refresh_due_at": NOW,
        }

    def intent_call_context(self, _status):
        return nullcontext()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["command_run", "task_claim"])
async def test_actual_intent_boundary_warns_on_minimal_success_then_blocks(monkeypatch, operation):
    if "intent" not in roles.AttachInput.model_fields:
        pytest.skip("Requires the separately coordinated intent integration candidate")
    raw = (
        _run()
        if operation == "command_run"
        else {"ok": True, "action": "claim", "task": _task(snapshot=True), "warnings": []}
    )
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
    server = _server(
        monkeypatch, "executor", mesh=_IntentBoundaryFixture(), **{service_method: raw}
    )
    data, response = await _call(server, operation, arguments)
    assert data["ok"] is True
    assert "next write will be blocked" in (response.meta or {}).get("intent_warning", "")
    assert any("next write will be blocked" in item.text for item in response.content[1:])
    blocked, _ = await _call(server, operation, arguments)
    assert blocked["ok"] is False
    assert blocked["error"]["code"] == "intent_stale"
    assert blocked["error"]["outcome"] == "not_committed"


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,arguments",
    [
        ("command_run", {"command": 42}),
        ("task_claim", {"action": "claim", "namespace": "acceptance", "task_id": "ONE"}),
        ("command_read", {}),
    ],
)
async def test_invalid_requests_are_small_actionable_failures(monkeypatch, name, arguments):
    data, _ = await _call(_server(monkeypatch, "executor"), name, arguments)
    assert data["ok"] is False
    error = data["error"]
    assert all(error.get(key) for key in ("code", "message", "outcome", "retry"))
    assert error.get("details")
    encoded = json.dumps(data)
    assert len(encoded) < 8192
    assert '"$defs"' not in encoded and '"oneOf"' not in encoded


@pytest.mark.asyncio
async def test_claim_revision_conflict_keeps_repair_and_does_not_claim(monkeypatch, tmp_path):
    path = tmp_path / "claim-revision.sqlite3"
    await SqliteRepository(path, tmp_path / "output.sqlite3").initialize()
    store = TaskStore(path)
    coordinator = TaskCoordinator(store)
    created = await coordinator.mutate(
        PUBLIC_NAME,
        action="create",
        namespace="acceptance",
        task_id="ONE",
        title="CAS",
        isolation_hint="none",
    )
    assert created["ok"] is True
    before = await store.get_task("acceptance", "ONE")
    claims_before = await store.active_claims("acceptance", "ONE")
    failed = await coordinator.mutate(
        PUBLIC_NAME,
        action="claim",
        namespace="acceptance",
        task_id="ONE",
        claim_intent="review",
        expected_revision=before["revision"] + 1,
    )
    assert failed["ok"] is False
    data, _ = await _call(
        _server(monkeypatch, "executor", task=failed),
        "task_claim",
        {
            "action": "claim",
            "namespace": "acceptance",
            "task_id": "ONE",
            "claim_intent": "review",
            "expected_revision": before["revision"] + 1,
        },
    )
    assert data["ok"] is False
    assert data["error"]["details"]["current_revision"] == before["revision"]
    assert data["error"]["outcome"] == "not_committed"
    assert data["error"]["retry"]
    assert await store.get_task("acceptance", "ONE") == before
    assert await store.active_claims("acceptance", "ONE") == claims_before


def test_run_strict_schema_validates_new_shape_and_rejects_removed_fields(monkeypatch):
    server = _server(monkeypatch, "executor")
    model = server._tool_manager.get_tool("command_run").fn_metadata.output_model
    valid = {"ok": True, "remaining_seconds": 0, "cmd_hash": "aabbccdd", "status": "queued"}
    model.model_validate(valid)
    for key, value in {
        "action": "run",
        "command": {},
        "identity": {},
        "coordination": {},
        "session_lifecycle": {},
        "lines": [],
        "exit_code": 0,
        "execution_started": False,
        "queue_position": 0,
    }.items():
        with pytest.raises(ValidationError):
            model.model_validate({**valid, key: value})


class _RunFixtureRepo:
    def __init__(self):
        self.record = None

    async def create(self, cmd, *, cmd_hash, status, queue_id, **_kwargs):
        self.record = SimpleNamespace(
            cmd=cmd,
            cmd_hash=cmd_hash,
            status=status,
            queue_id=queue_id,
            exit_code=None,
            claimed_at=None,
            started_at=None,
            finished_at=None,
        )
        return self.record

    async def get(self, _cmd_hash):
        return self.record

    async def queue_position(self, _cmd_hash):
        return 1

    async def queue_snapshot(self, _workers):
        return [{"queue_id": 1, "running": None, "queued": 1}]


class _RunFixtureLifecycle:
    @asynccontextmanager
    async def operation_guard(self, _logical_agent_id):
        yield


class _RunFixtureBackend(PersistentBackend):
    async def _execution_authority(self, *_args, **_kwargs):
        return SimpleNamespace(hard_expires_at=NOW, authority_node_id="fixture"), None

    async def _task_refs(self, _agent):
        return []


@pytest.mark.asyncio
async def test_run_never_polls_or_sleeps_to_obtain_a_later_status(monkeypatch):
    import asyncio

    import terminal_mcp.core.persistent_backend as backend_module

    repo = _RunFixtureRepo()

    async def submit(_record):
        return None

    async def forbidden_wait(_delay):
        raise AssertionError("command acceptance must not wait for a later status")

    terminal = SimpleNamespace(queue_workers=1, submit=submit)
    service = SimpleNamespace(
        repo=repo,
        terminal=terminal,
        task_coordinator=None,
        task_store=None,
        agent_policy=SimpleNamespace(command_preview_chars=80),
    )
    backend = _RunFixtureBackend(service, _RunFixtureLifecycle())
    monkeypatch.setattr(
        backend_module,
        "asyncio",
        SimpleNamespace(get_running_loop=asyncio.get_running_loop, sleep=forbidden_wait),
    )
    result = await backend.run(
        "fixture command",
        logical_agent_id="fixture-agent",
        work_session_id="fixture-session",
        session_epoch=1,
        queue_id=1,
        task_scope="none",
    )
    assert result["ok"] is True, result
    assert result["status"] == "queued"
    assert "postcommit_warning" not in result, result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,exit_code",
    [
        ("queued", None),
        ("running", None),
        ("completed", 0),
        ("failed", 7),
        ("cancelled", 130),
    ],
)
async def test_read_preserves_output_and_known_exit_after_finish_or_cancel(
    monkeypatch, status, exit_code
):
    raw = {
        "ok": True,
        "cmd_hash": "aabbccdd",
        "status": status,
        "exit_code": exit_code,
        "queue_id": 3,
        "lines": ["stdout: fixture", "stderr: fixture"],
        "overall_lines_count": 4,
        "displayed_lines_count": 2,
        "has_more": True,
        "next_cursor": "fixture-cursor",
        "output_truncated": False,
        "output_retained": True,
        "output_bytes": 0,
    }
    data, _ = await _call(
        _server(monkeypatch, "executor", cmd=raw),
        "command_read",
        {"cmd_hash": "aabbccdd", "limit": 2},
    )
    assert data["ok"] is True, data
    command = data.get("command") if isinstance(data.get("command"), dict) else data
    assert command["status"] == status
    if exit_code is not None:
        assert command["exit_code"] == exit_code
        assert type(command["exit_code"]) is int
    assert data["lines"] == raw["lines"]
    assert data["has_more"] is True
    assert data["next_cursor"] == raw["next_cursor"]
    assert data["output_retained"] is True and data["output_truncated"] is False
    assert data["output_bytes"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("retained,truncated", [(False, False), (True, True)])
async def test_read_retention_and_truncation_are_meaningful_booleans(
    monkeypatch, retained, truncated
):
    raw = {
        "ok": True,
        "cmd_hash": "aabbccdd",
        "status": "completed",
        "exit_code": 0,
        "lines": [],
        "overall_lines_count": 0,
        "displayed_lines_count": 0,
        "has_more": False,
        "output_truncated": truncated,
        "output_retained": retained,
        "output_bytes": 0,
    }
    data, _ = await _call(
        _server(monkeypatch, "executor", cmd=raw), "command_read", {"cmd_hash": "aabbccdd"}
    )
    assert data["ok"] is True, data
    assert data["has_more"] is False
    assert data["output_retained"] is retained and data["output_truncated"] is truncated
    assert data["output_bytes"] == 0


@pytest.mark.asyncio
async def test_read_surfaces_required_incoming_message_without_duplication(monkeypatch):
    msg = _message("ack")
    raw = {
        "ok": True,
        "cmd_hash": "aabbccdd",
        "status": "running",
        "lines": [],
        "messages": [msg],
        "pending_messages": [deepcopy(msg)],
    }
    data, _ = await _call(
        _server(monkeypatch, "executor", cmd=raw), "command_read", {"cmd_hash": "aabbccdd"}
    )
    assert data["ok"] is True, data
    assert data.get("messages") == [
        {
            "id": msg["message_hash"],
            "sender": PUBLIC_NAME,
            "text": "incoming",
            "requirements": "ack",
        }
    ]
    assert "coordination" not in data and "pending_messages" not in data


@pytest.mark.asyncio
async def test_read_includes_original_command_body_and_execution_error(monkeypatch):
    body = "printf '%s\\n' 'original body'; printf '%s\\n' 'stderr' >&2; exit 7"
    raw = {
        "ok": True,
        "cmd_hash": "aabbccdd",
        "cmd": body,
        "status": "failed",
        "exit_code": 7,
        "error": "fixture execution error",
        "lines": ["original body", "stderr"],
    }
    data, _ = await _call(
        _server(monkeypatch, "executor", cmd=raw), "command_read", {"cmd_hash": "aabbccdd"}
    )
    assert data["ok"] is True, data
    assert data["command"]["cmd"] == body
    assert data["command"]["error"] == raw["error"]
    assert data["command"]["exit_code"] == 7
    assert data["lines"] == raw["lines"]
