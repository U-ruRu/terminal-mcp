import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.core.persistent_fleet import PersistentFleetBridge
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.persistent_agents import PersistentAgentStore, PersistentStoreError
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
async def test_remote_revoke_releases_without_local_session_and_fences_late_claim(remote):
    repo, bridge, fields = remote
    assert await bridge.store.get_work_session("ws_foreign") is None
    args = dict(
        logical_agent_id="la_foreign",
        work_session_id="ws_foreign",
        session_epoch=3,
        reason="session_end",
    )
    assert await bridge.receive_revoke(**args) == []
    assert await bridge.task_store.active_claims("test", "remote") == []
    item = await bridge.task_store.get_task("test", "remote")
    assert item["state"] == "ready" and item["checkpoint"] == {"keep": 1}
    with pytest.raises(PersistentStoreError, match="session_not_active"):
        await bridge.task_store.claim_owner(
            "test", "remote", ClaimOwner.logical_agent("la_foreign"), lease=fields
        )
    successor = {**fields, "work_session_id": "ws_next", "session_epoch": 4}
    await bridge.task_store.claim_owner(
        "test", "remote", ClaimOwner.logical_agent("la_foreign"), lease=successor
    )
    before = await bridge.task_store.active_claims("test", "remote")
    assert await bridge_for(repo).receive_revoke(**args) == []
    assert await bridge.task_store.active_claims("test", "remote") == before


@pytest.mark.asyncio
async def test_remote_claim_only_expiry_survives_restart_and_needs_no_command(remote):
    repo, _, _ = remote
    with sqlite3.connect(repo.path) as db:
        db.execute(
            "UPDATE work_claim_leases SET hard_expires_at=?",
            (utc_text(utc_now() - timedelta(seconds=1)),),
        )
        assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 0
    restarted = bridge_for(repo)
    health = await TaskCoordinator(restarted.task_store).health()
    assert health["stale_claims"] == 1 and health["live_claims"] == 0
    await restarted.reconcile_remote_expiry()
    assert len(restarted.execution_fence.calls) == 1
    assert await restarted.task_store.active_claims("test", "remote") == []
    assert (await restarted.task_store.get_task("test", "remote"))["state"] == "ready"
    assert (await TaskCoordinator(restarted.task_store).health())["stale_claims"] == 0


@pytest.mark.asyncio
async def test_failed_fence_retains_claim_but_blocks_late_admission_and_retries(remote):
    repo, bridge, fields = remote
    bridge.execution_fence.blockers = [{"kind": "command", "cmd_hash": "still-running"}]
    args = dict(
        logical_agent_id="la_foreign",
        work_session_id="ws_foreign",
        session_epoch=3,
        reason="session_interrupt",
    )
    assert await bridge.receive_revoke(**args) == bridge.execution_fence.blockers
    assert await bridge.task_store.active_claims("test", "remote")
    with pytest.raises(PersistentStoreError, match="session_not_active"):
        await bridge.task_store.claim_owner(
            "test", "remote", ClaimOwner.logical_agent("la_foreign"), lease=fields
        )
    # Reap persisted revoke fences even before the original future hard deadline.
    restarted = bridge_for(repo)
    await restarted.reconcile_remote_expiry()
    assert await restarted.task_store.active_claims("test", "remote") == []


@pytest.mark.asyncio
async def test_runtime_absence_never_releases_claim_before_execution_fence(remote):
    _, bridge, fields = remote
    bridge.execution_fence = None
    result = await bridge.receive_revoke(
        logical_agent_id="la_foreign",
        work_session_id="ws_foreign",
        session_epoch=3,
        reason="hard_duration",
    )
    assert result[0]["kind"] == "runtime_unavailable"
    assert await bridge.task_store.active_claims("test", "remote")
    with pytest.raises(PersistentStoreError, match="session_not_active"):
        await bridge.task_store.claim_owner(
            "test", "remote", ClaimOwner.logical_agent("la_foreign"), lease=fields
        )
