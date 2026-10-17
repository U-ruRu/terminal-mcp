from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.core.session_execution import SessionExecutionError
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
            raise SessionExecutionError("stale_session")
        return None

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


@pytest.mark.asyncio
async def test_replay_still_checks_authority(tmp_path):
    backend = await make_backend(tmp_path)
    backend.authorized = False
    result = await backend.replay_command("run", "printf test", **identity())
    assert result["code"] == "stale_session" and backend.launches == 0
