import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.commands import CommandApplication
from terminal_mcp.application.requests import CmdRecoveryRequest, CmdRunRequest
from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.core.persistent_lifecycle import PersistentLifecycleError
from terminal_mcp.storage.persistent_agents import PersistentAgentStore
from terminal_mcp.storage.sqlite import SqliteRepository


@asynccontextmanager
async def guard(_agent):
    yield


class ReplayBackend(PersistentBackend):
    def __init__(self, store):
        self.lifecycle = SimpleNamespace(store=store, operation_guard=guard)
        self.launches = 0
        self.authorized = True
        self.result = None
        self.raise_failure = False

    async def _execution_authority(self, *args, **kwargs):
        if not self.authorized:
            raise PersistentLifecycleError("stale_session")
        return None, None

    async def _audit(self, *args, **kwargs):
        pass

    async def run(self, command, **kwargs):
        self.launches += 1
        if self.raise_failure:
            raise ValueError("uncertain failure after acceptance")
        return self.result or {"ok": True, "cmd_hash": f"cmd-{self.launches}", "status": "queued"}

    recovery = run


async def make_backend(tmp_path):
    repo = SqliteRepository(tmp_path / "state.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    await store.create_slot("la-test", "ReplayTest", "R123", authority_node_id="firstbyte")
    return ReplayBackend(store)


def identity():
    return {
        "logical_agent_id": "la-test",
        "work_session_id": "ws-test",
        "session_epoch": 1,
        "idempotency_key": "ws-test:rpc-request",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["run", "recovery"])
async def test_command_receipt_survives_backend_restart_and_rejects_changed_payload(
    tmp_path, action
):
    backend = await make_backend(tmp_path)
    first = await backend.replay_command(action, "printf test", **identity())
    assert backend.launches == 1
    restarted = ReplayBackend(backend.lifecycle.store)
    repeated = await restarted.replay_command(action, "printf test", **identity())
    assert repeated == first
    assert restarted.launches == 0
    changed = await restarted.replay_command(action, "printf different", **identity())
    assert changed["code"] == "idempotency_conflict"
    assert restarted.launches == 0
    restarted.authorized = False
    rejected = await restarted.replay_command(action, "printf test", **identity())
    assert rejected["code"] == "stale_session"
    assert restarted.launches == 0


@pytest.mark.asyncio
async def test_unknown_launch_outcome_remains_reserved(tmp_path):
    backend = await make_backend(tmp_path)
    backend.result = {"ok": False, "code": "run_failed", "error": "opaque failure"}
    first = await backend.replay_command("run", "printf test", **identity())
    assert first["code"] == "run_failed"
    repeated = await backend.replay_command("run", "printf test", **identity())
    assert repeated["code"] == "idempotency_in_progress"
    assert backend.launches == 1


@pytest.mark.asyncio
async def test_uncertain_exception_retains_launch_reservation(tmp_path):
    backend = await make_backend(tmp_path)
    backend.raise_failure = True
    with pytest.raises(ValueError):
        await backend.replay_command("run", "printf test", **identity())
    repeated = await backend.replay_command("run", "printf test", **identity())
    assert repeated["code"] == "idempotency_in_progress"
    assert backend.launches == 1


@pytest.mark.asyncio
async def test_confirmed_precommit_failure_releases_launch_reservation(tmp_path):
    backend = await make_backend(tmp_path)
    backend.result = {"ok": False, "code": "input_validation_failed", "error": "Repair input"}
    await backend.replay_command("run", "printf test", **identity())
    backend.result = None
    assert (await backend.replay_command("run", "printf test", **identity()))["ok"] is True
    assert backend.launches == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["run", "recovery"])
async def test_application_uses_server_owned_replay_identity(action):
    captured = {}

    class Backend:
        async def replay_command(self, *args, **kwargs):
            captured.update(args=args, **kwargs)
            return {"ok": True, "cmd_hash": "one-command", "status": "queued"}

    app = CommandApplication(SimpleNamespace(persistent=Backend()), object())
    actor = ActorContext(transport="mcp", endpoint_role="executor", request_id="rpc-id")
    request = (
        CmdRunRequest(action="run", command="printf test", task_scope="none")
        if action == "run"
        else CmdRecoveryRequest(action="recovery", command="printf test")
    )
    await app._launch(
        actor,
        {"logical_agent_id": "la-test", "work_session_id": "ws-test", "session_epoch": 1},
        request,
    )
    assert captured["idempotency_key"] == "ws-test:rpc-id"
    assert captured["args"] == (action, "printf test")


@pytest.mark.asyncio
async def test_concurrent_replay_launches_one_command(tmp_path):
    backend = await make_backend(tmp_path)
    results = await asyncio.gather(
        *(backend.replay_command("run", "printf test", **identity()) for _ in range(5))
    )
    assert backend.launches == 1
    for result in results:
        assert result.get("ok") is True or result["code"] == "idempotency_in_progress"
    receipt = await backend.replay_command("run", "printf test", **identity())
    assert receipt["cmd_hash"] == "cmd-1"
    assert backend.launches == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["run", "recovery"])
async def test_reused_zero_id_never_returns_a_stale_command_receipt(action):
    class Backend:
        def __init__(self):
            self.launches = 0

        async def run(self, *args, **kwargs):
            self.launches += 1
            return {"ok": True, "cmd_hash": str(self.launches)}

        recovery = run

        async def replay_command(self, *args, **kwargs):
            pytest.fail("Zero is reused by the stateless connector, not a unique operation ID")

    backend = Backend()
    app = CommandApplication(SimpleNamespace(persistent=backend), object())
    actor = ActorContext(transport="mcp", endpoint_role="executor", request_id="0")
    request = (
        CmdRunRequest(action="run", command="printf test", task_scope="none")
        if action == "run"
        else CmdRecoveryRequest(action="recovery", command="printf test")
    )
    ident = {"logical_agent_id": "la-test", "work_session_id": "ws-test", "session_epoch": 1}
    first = await app._launch(actor, ident, request)
    second = await app._launch(actor, ident, request)
    assert first["cmd_hash"] != second["cmd_hash"]
    assert backend.launches == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", ["0", "1", "reused-connector-id", "", 0])
async def test_reused_request_id_scopes_task_replays_by_normalized_mutation(request_id):
    from terminal_mcp.application.task_requests import TaskCommentRequest
    from terminal_mcp.application.tasks import TaskApplication

    class Gate:
        async def identity(self, *args, **kwargs):
            return {
                "logical_agent_id": "la-test",
                "work_session_id": "ws-test",
                "session_epoch": 1,
            }, None

    class Backend:
        def __init__(self):
            self.keys = []

        async def task(self, **kwargs):
            self.keys.append(kwargs["idempotency_key"])
            return {"ok": True, "action": "comment"}

    backend = Backend()
    app = TaskApplication(SimpleNamespace(persistent=backend), Gate())
    actor = ActorContext(transport="mcp", endpoint_role="executor", request_id=request_id)
    for text in ("First mutation", "Different mutation", "First mutation"):
        await app.task(
            actor,
            TaskCommentRequest(
                action="comment", namespace="test", task_id="one", comment_text=text
            ),
        )
    assert backend.keys[0] != backend.keys[1]
    assert backend.keys[0] == backend.keys[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["run", "recovery"])
async def test_remote_receipts_authorize_and_replay_without_local_authority_slot(tmp_path, action):
    repo = SqliteRepository(tmp_path / "remote.sqlite3")
    await repo.initialize()
    backend = ReplayBackend(PersistentAgentStore(repo.path))
    args = {**identity(), "logical_agent_id": "foreign-la"}
    first = await backend.replay_command(action, "printf remote", **args)
    assert first["ok"] and backend.launches == 1
    assert await backend.lifecycle.store.get_slot("foreign-la") is None
    restarted = ReplayBackend(backend.lifecycle.store)
    assert await restarted.replay_command(action, "printf remote", **args) == first
    assert restarted.launches == 0
    restarted.authorized = False
    denied = await restarted.replay_command(action, "printf remote", **args)
    assert denied["code"] == "stale_session"
    assert restarted.launches == 0
