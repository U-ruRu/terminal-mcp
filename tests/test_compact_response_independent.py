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

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.mcp import roles

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


def _current_role_service():
    """Current role envelope with already-satisfied intent and no attached session."""

    async def observe_intent_call(_actor, *, require_intent):
        return {}

    mesh = SimpleNamespace(
        store=SimpleNamespace(attached_slot=lambda _key: None, touch=lambda _key: None),
        connection_key=lambda _actor: "current-role-fixture",
        observe_intent_call=observe_intent_call,
        intent_call_context=lambda _status: nullcontext(),
    )
    return SimpleNamespace(persistent=None, access_mesh=mesh)


def _server(monkeypatch, role, *, mesh=None, **responses):
    async def touch_provider(*_args, **_kwargs):
        return None

    service = _current_role_service()
    if mesh is not None:
        service.access_mesh = mesh
    app = SimpleNamespace(
        service=service,
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
@pytest.mark.parametrize(
    "value", [{"metadata": {"flag": False, "zero": 0, "null": None}}, [], '{"literal":true}', ""]
)
async def test_checkpoint_snapshot_preserves_the_original_json_type(monkeypatch, value):
    snapshot = {
        **_task(snapshot=True),
        "latest_checkpoint": {
            "checkpoint": value,
            "text": "Preview",
            "author": PUBLIC_NAME,
            "created_at": NOW,
            "revision": 3,
        },
    }
    server = _server(monkeypatch, "coordinator", observe={"ok": True, "task": snapshot})

    data, _ = await _call(
        server, "task_get", {"namespace": "acceptance", "task_id": "ONE", "detail": "snapshot"}
    )

    assert data["ok"] is True
    checkpoint = data["task"]["latest_checkpoint"]["checkpoint"]
    assert checkpoint == value and type(checkpoint) is type(value)


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
