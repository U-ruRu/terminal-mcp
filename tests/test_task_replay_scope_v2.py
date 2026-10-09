"""Automatic task replay is scoped by caller cycle and canonical domain arguments."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.task_requests import TaskCheckpointRequest
from terminal_mcp.application.tasks import TaskApplication
from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.storage.persistent_agents import PersistentAgentStore
from terminal_mcp.storage.sqlite import SqliteRepository


class Gate:
    def __init__(self):
        self.allowed = True
        self.identity_value = {
            "logical_agent_id": "la-replay",
            "work_session_id": "ws-replay",
            "session_epoch": 1,
        }

    async def identity(self, *args):
        return (
            (self.identity_value, None)
            if self.allowed
            else (None, {"ok": False, "code": "stale_session", "error": "stale_session"})
        )


class Backend(PersistentBackend):
    def __init__(self, store):
        self.lifecycle = SimpleNamespace(store=store)
        self.keys = []
        self.mutations = 0

    async def _audit(self, *args, **kwargs):
        pass

    async def task(self, **kwargs):
        self.keys.append(kwargs["idempotency_key"])
        request = {key: kwargs[key] for key in ("action", "namespace", "task_id", "checkpoint")}

        async def commit():
            self.mutations += 1
            return {"ok": True, "task": {"revision": self.mutations, **request}}

        key = kwargs["idempotency_key"]
        if key is None:
            return await commit()
        return await self._idempotent(
            kwargs["logical_agent_id"], f"task.{kwargs['action']}", key, request, commit
        )


async def setup(tmp_path):
    repo = SqliteRepository(tmp_path / "replay.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    await store.create_slot("la-replay", "Replay", "R444", authority_node_id="firstbyte")
    backend = Backend(store)
    gate = Gate()
    app = TaskApplication(SimpleNamespace(persistent=backend), gate)
    actor = ActorContext(transport="mcp", request_id="same-nonzero-id", endpoint_role="coordinator")
    return app, backend, gate, actor


def request(value):
    return TaskCheckpointRequest(
        action="checkpoint",
        namespace="test",
        task_id="one",
        checkpoint=value,
    )


@pytest.mark.asyncio
async def test_same_id_different_checkpoint_commits_and_original_replays_across_restart(tmp_path):
    app, backend, gate, actor = await setup(tmp_path)
    first = await app.task(actor, request({"step": 1, "evidence": {"a": 1, "b": 2}}))
    second = await app.task(actor, request({"step": 2}))
    assert first["ok"] and second["ok"]
    assert backend.mutations == 2
    assert backend.keys[0] is backend.keys[1] is None
    restarted = Backend(backend.lifecycle.store)
    app = TaskApplication(SimpleNamespace(persistent=restarted), gate)
    replay = await app.task(actor, request({"evidence": {"b": 2, "a": 1}, "step": 1}))
    assert replay["ok"]
    assert restarted.mutations == 1
    assert restarted.keys[0] is None


@pytest.mark.asyncio
async def test_transport_id_and_normalized_payload_are_both_part_of_replay_identity(tmp_path):
    app, backend, _, actor = await setup(tmp_path)
    value = request("checkpoint")
    first = await app.task(actor, value)
    second = await app.task(actor, value)
    assert second["ok"] and second["task"]["revision"] != first["task"]["revision"]
    assert backend.mutations == 2
    await app.task(replace(actor, request_id="another-id"), value)
    assert backend.mutations == 3
    assert backend.keys == [None, None, None]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("work_session_id", "ws-successor"),
        ("session_epoch", 2),
    ],
)
async def test_successor_cycle_never_replays_predecessor_mutation(tmp_path, field, value):
    app, backend, gate, actor = await setup(tmp_path)
    await app.task(actor, request("checkpoint"))
    gate.identity_value = {**gate.identity_value, field: value}
    await app.task(actor, request("checkpoint"))
    assert backend.mutations == 2
    assert backend.keys[0] is backend.keys[1] is None


@pytest.mark.asyncio
async def test_replay_still_requires_live_gate_and_missing_metadata_does_not_invent_a_key(tmp_path):
    app, backend, gate, actor = await setup(tmp_path)
    await app.task(actor, request("checkpoint"))
    gate.allowed = False
    denied = await app.task(actor, request("checkpoint"))
    assert denied["ok"] is False
    assert denied["code"] == "stale_session"
    assert len(backend.keys) == 1
    gate.allowed = True
    for _ in range(2):
        await app.task(replace(actor, request_id=None), request("checkpoint"))
    assert backend.keys[-2:] == [None, None]
    assert backend.mutations == 3
