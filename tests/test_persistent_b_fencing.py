"""Native session authority and exact-session scheduler fencing."""

import pytest
from native_runtime_helpers import native_agent
from persistent_app_test_helpers import NoNativeExecutionPort

from terminal_mcp.application.command_scheduler import CommandScheduler
from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.core.native_admission import (
    NativeWriteAdmission,
    NativeWriteAdmissionError,
    native_write_admission,
)
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
@pytest.mark.parametrize("status", ["queued", "running"])
@pytest.mark.parametrize("transition", ["ended", "namespace_changed"])
async def test_command_create_rechecks_captured_native_authority(tmp_path, status, transition):
    repo, _ = await repo_and_store(tmp_path)
    store, identity = native_agent(repo.path)
    admission = NativeWriteAdmission(
        namespace_id=store.namespace_id,
        logical_agent_id=identity["logical_agent_id"],
        work_session_id=identity["work_session_id"],
        session_epoch=identity["session_epoch"],
        operation=(
            ManagedOperation.COMMAND_RECOVERY
            if status == "running"
            else ManagedOperation.COMMAND_RUN
        ),
    )
    with store._connect() as db:
        if transition == "ended":
            db.execute("UPDATE logical_agent_work_sessions SET state='ended'")
        else:
            db.execute("UPDATE access_mesh_registry_scope SET namespace_id='other-mesh'")

    with native_write_admission(admission), pytest.raises(NativeWriteAdmissionError) as error:
        await repo.create(
            "printf must-not-be-recorded",
            cmd_hash="rejected",
            status=status,
            agent_id=identity["logical_agent_id"],
            logical_agent_id=identity["logical_agent_id"],
            work_session_id=identity["work_session_id"],
            session_epoch=identity["session_epoch"],
            command_type="persistent_recovery" if status == "running" else "persistent_run",
        )

    assert error.value.code == (
        "session_not_active" if transition == "ended" else "mesh_namespace_mismatch"
    )
    assert await repo.get("rejected") is None
    assert await repo.persistent_attribution("rejected") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("native_state", ["active", "ended", "missing_registry"])
async def test_pid_write_rechecks_native_authority_without_request_context(tmp_path, native_state):
    repo, _ = await repo_and_store(tmp_path)
    store, identity = native_agent(repo.path)
    command = await repo.create(
        "printf must-wait-for-admission",
        status="running",
        agent_id=identity["logical_agent_id"],
        logical_agent_id=identity["logical_agent_id"],
        work_session_id=identity["work_session_id"],
        session_epoch=identity["session_epoch"],
        command_type="persistent_recovery",
    )
    with store._connect() as db:
        if native_state == "ended":
            db.execute("UPDATE logical_agent_work_sessions SET state='ended'")
        elif native_state == "missing_registry":
            db.execute("DROP TABLE access_mesh_registry_scope")

    accepted = await repo.set_pid(command.cmd_hash, 12345)

    current = await repo.get(command.cmd_hash)
    assert accepted is (native_state == "active")
    assert current.pid == (12345 if accepted else None)
    assert current.status == ("running" if accepted else "cancelled")
    if not accepted:
        assert current.error == "persistent.session_fenced"


@pytest.mark.asyncio
async def test_pid_write_preserves_the_request_namespace(tmp_path):
    repo, _ = await repo_and_store(tmp_path)
    store, identity = native_agent(repo.path)
    admission = NativeWriteAdmission(
        namespace_id=store.namespace_id,
        logical_agent_id=identity["logical_agent_id"],
        work_session_id=identity["work_session_id"],
        session_epoch=identity["session_epoch"],
        operation=ManagedOperation.COMMAND_RECOVERY,
    )
    command = await repo.create(
        "printf old-namespace",
        status="running",
        agent_id=identity["logical_agent_id"],
        logical_agent_id=identity["logical_agent_id"],
        work_session_id=identity["work_session_id"],
        session_epoch=identity["session_epoch"],
        command_type="persistent_recovery",
    )
    with store._connect() as db:
        db.execute("UPDATE access_mesh_registry_scope SET namespace_id='other-mesh'")

    with native_write_admission(admission):
        assert await repo.set_pid(command.cmd_hash, 12345) is False

    current = await repo.get(command.cmd_hash)
    assert current.status == "cancelled"
    assert current.pid is None


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
