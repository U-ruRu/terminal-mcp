"""Native session authority and exact-session scheduler fencing."""

import pytest
from persistent_app_test_helpers import NoNativeExecutionPort

from terminal_mcp.application.command_scheduler import CommandScheduler
from terminal_mcp.core.persistent_execution import PersistentExecutionFence
from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.persistent_agents import PersistentAgentStore
from terminal_mcp.storage.sqlite import SqliteRepository


def _scheduler(repo):
    return CommandScheduler(repo, NoNativeExecutionPort(), 0.1)


async def repo_and_store(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    return repo, PersistentAgentStore(repo.path)


@pytest.mark.asyncio
@pytest.mark.parametrize("native_state", ["active", "ended", "missing_registry"])
async def test_queue_claim_requires_the_current_native_session(tmp_path, native_state):
    from native_runtime_helpers import native_agent

    repo, _ = await repo_and_store(tmp_path)
    store, identity = native_agent(repo.path)
    if native_state == "ended":
        with store._connect() as db:
            db.execute("UPDATE logical_agent_work_sessions SET state='ended'")
    if native_state == "missing_registry":
        with store._connect() as db:
            db.execute("DROP TABLE access_mesh_native_owners")
    command = await repo.create(
        "printf native",
        queue_id=1,
        agent_id=identity["logical_agent_id"],
        logical_agent_id=identity["logical_agent_id"],
        work_session_id=identity["work_session_id"],
        session_epoch=identity["session_epoch"],
        command_type="persistent_run",
    )
    claimed = await repo.claim_next(1)
    if native_state != "active":
        assert claimed is None
        assert (await repo.get(command.cmd_hash)).status == "cancelled"
    else:
        assert claimed is not None and claimed.cmd_hash == command.cmd_hash
        assert claimed.status == "running"


@pytest.mark.asyncio
async def test_generic_cancel_cannot_bypass_persistent_work_session_fence(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    terminal = _scheduler(repo)
    service = TerminalService(repo, terminal, 5000)
    command = await repo.create(
        "sleep 30",
        queue_id=1,
        agent_id="logical-1",
        logical_agent_id="logical-1",
        work_session_id="ws-1",
        session_epoch=1,
        command_type="persistent_run",
    )

    result = await service.cancel(command.cmd_hash)
    assert result["ok"] is False
    assert result["error"] == "cancel.persistent: exact work-session context required"
    assert (await repo.get(command.cmd_hash)).status == "queued"


@pytest.mark.asyncio
async def test_persistent_fence_observes_session_blockers_without_cancelling(tmp_path):
    repo = SqliteRepository(tmp_path / "drain.sqlite3", tmp_path / "drain-output.sqlite3")
    await repo.initialize()
    terminal = _scheduler(repo)
    queued = await repo.create(
        "printf queued",
        queue_id=1,
        agent_id="logical-1",
        logical_agent_id="logical-1",
        work_session_id="ws-1",
        session_epoch=1,
        command_type="persistent_run",
    )
    running = await repo.create(
        "printf running",
        status="running",
        queue_id=1,
        agent_id="logical-1",
        logical_agent_id="logical-1",
        work_session_id="ws-1",
        session_epoch=1,
        command_type="persistent_run",
    )
    terminal._mark_claimed(running.cmd_hash)
    fence = PersistentExecutionFence(repo, terminal, None)

    blockers = await fence.blockers_for_session("logical-1", "ws-1", 1)
    assert {item["command_hash"] for item in blockers} == {queued.cmd_hash, running.cmd_hash}
    assert {item["kind"] for item in blockers} == {"queued_command", "starting_command"}
    assert (await repo.get(queued.cmd_hash)).status == "queued"
    assert (await repo.get(running.cmd_hash)).status == "running"
    assert terminal.cancel_requested == set()
    terminal._release_claimed(running.cmd_hash)
