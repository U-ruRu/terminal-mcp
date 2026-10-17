from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.core.persistent_fleet import PersistentFleetBridge
from terminal_mcp.storage.persistent_agents import PersistentAgentStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


class Fence:
    def __init__(self):
        self.calls = []
        self.blockers = []

    async def revoke_session(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.blockers


def bridge_for(repo):
    tasks = TaskStore(repo.path)
    bridge = PersistentFleetBridge(
        SimpleNamespace(instance_id="peer"), PersistentAgentStore(repo.path), repo, object(), tasks
    )
    bridge.execution_fence = Fence()
    return bridge


@pytest_asyncio.fixture
async def remote(tmp_path):
    repo = SqliteRepository(tmp_path / "peer.sqlite3")
    await repo.initialize()
    bridge = bridge_for(repo)
    await bridge.task_store.create_task("test", "remote", "foreign work", checkpoint={"keep": 1})
    fields = dict(
        work_session_id="ws_foreign",
        session_epoch=3,
        hard_expires_at=utc_text(utc_now() + timedelta(minutes=5)),
    )
    await bridge.task_store.claim_owner(
        "test", "remote", ClaimOwner.logical_agent("la_foreign"), lease=fields
    )
    return repo, bridge, fields


@pytest.mark.asyncio
async def test_failed_execution_fence_retains_the_claim_for_cleanup(remote):
    _, bridge, _ = remote
    bridge.execution_fence.blockers = [{"kind": "command", "cmd_hash": "still-running"}]

    blockers = await bridge.receive_revoke(
        logical_agent_id="la_foreign",
        work_session_id="ws_foreign",
        session_epoch=3,
        reason="session_interrupt",
    )

    assert blockers == bridge.execution_fence.blockers
    claims = await bridge.task_store.active_claims("test", "remote")
    assert [claim["owner_id"] for claim in claims] == ["la_foreign"]
