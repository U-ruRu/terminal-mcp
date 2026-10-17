"""Native cleanup retains task ownership while the execution fence has blockers."""

import pytest
from native_runtime_helpers import native_agent

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.core.access_mesh_grants import AccessSlotEvent
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


class Fence:
    async def revoke_session(self, *args, **kwargs):
        return [{"kind": "command", "cmd_hash": "still-running"}]


@pytest.mark.asyncio
async def test_failed_execution_fence_retains_the_claim_for_cleanup(tmp_path):
    repo = SqliteRepository(tmp_path / "native.sqlite3")
    await repo.initialize()
    store, identity = native_agent(repo.path, "claim-agent")
    tasks = TaskStore(repo.path)
    await tasks.create_task("test", "native", "work", checkpoint={"keep": 1})
    await tasks.claim_owner("test", "native", ClaimOwner.logical_agent("claim-agent"))
    store.apply_event(
        AccessSlotEvent("local", identity["slot_id"], "claim-agent", 2, "stop", "SlotDeleted"),
        authenticated_peer_id="local",
    )
    mesh = AccessMeshApplication(store, task_store=tasks, execution_fence=Fence())
    await mesh.cleanup()
    assert [c["owner_id"] for c in await tasks.active_claims("test", "native")] == ["claim-agent"]
    assert len(store.pending_cleanup()) == 1
    assert (await tasks.get_task("test", "native"))["checkpoint"] == {"keep": 1}
