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
async def test_same_transport_id_never_deduplicates_command(tmp_path, action):
    backend = await make_backend(tmp_path)
    first = await backend.replay_command(action, "printf test", **identity())
    second = await backend.replay_command(action, "printf test", **identity())
    assert first["cmd_hash"] != second["cmd_hash"]
    assert backend.launches == 2
    restarted = ReplayBackend(backend.lifecycle.store)
    third = await restarted.replay_command(action, "printf test", **identity())
    assert third["ok"] and restarted.launches == 1


@pytest.mark.asyncio
async def test_concurrent_calls_are_independent(tmp_path):
    backend = await make_backend(tmp_path)
    results = await asyncio.gather(
        *(backend.replay_command("run", "printf test", **identity()) for _ in range(5))
    )
    assert backend.launches == 5
    assert len({result["cmd_hash"] for result in results}) == 5


@pytest.mark.asyncio
async def test_precommit_failure_can_be_retried_without_stale_reservation(tmp_path):
    backend = await make_backend(tmp_path)
    backend.result = {"ok": False, "code": "input_validation_failed"}
    assert not (await backend.replay_command("run", "invalid", **identity()))["ok"]
    backend.result = None
    second = await backend.replay_command("run", "printf valid", **identity())
    assert second["ok"] and backend.launches == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["run", "recovery"])
@pytest.mark.parametrize("request_id", ["0", "1", "reused-connector-id", ""])
async def test_application_never_replays_transport_id(action, request_id):
    class Backend:
        def __init__(self):
            self.count = 0

        async def run(self, *args, **kwargs):
            self.count += 1
            return {"ok": True, "cmd_hash": str(self.count)}

        recovery = run

        async def replay_command(self, *args, **kwargs):
            pytest.fail("Transport replay is forbidden")

    backend = Backend()
    app = CommandApplication(SimpleNamespace(persistent=backend), object())
    actor = ActorContext(transport="mcp", endpoint_role="executor", request_id=request_id)
    request = (
        CmdRunRequest(action="run", command="printf test", task_scope="none")
        if action == "run"
        else CmdRecoveryRequest(action="recovery", command="printf test")
    )
    caller = {"logical_agent_id": "la-test", "work_session_id": "ws-test", "session_epoch": 1}
    one = await app._launch(actor, caller, request)
    two = await app._launch(actor, caller, request)
    assert one["cmd_hash"] != two["cmd_hash"] and backend.count == 2


@pytest.mark.asyncio
async def test_replay_still_checks_authority(tmp_path):
    backend = await make_backend(tmp_path)
    backend.authorized = False
    result = await backend.replay_command("run", "printf test", **identity())
    assert result["code"] == "stale_session" and backend.launches == 0
