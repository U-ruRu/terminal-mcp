"""Persistent scheduler/store/lifecycle wiring with a forbidden native neighbor."""

from datetime import timedelta

import pytest
from persistent_app_test_helpers import NoNativeExecutionPort

from terminal_mcp.application.command_scheduler import CommandScheduler
from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_admission import (
    VerifiedAdmissionContext,
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
async def test_valid_remote_permit_allows_queue_claim(tmp_path):
    repo, store = await repo_and_store(tmp_path)
    now = utc_now()
    future = utc_text(now + timedelta(minutes=5))
    permit_expiry = utc_text(now + timedelta(seconds=30))

    command = await repo.create(
        "printf remote-ok",
        queue_id=1,
        agent_id="logical-remote",
        logical_agent_id="logical-remote",
        work_session_id="ws-remote",
        session_epoch=7,
        command_type="persistent_run",
    )
    await store.record_command_permit(
        command.cmd_hash,
        {
            "logical_agent_id": "logical-remote",
            "work_session_id": "ws-remote",
            "session_epoch": 7,
            "authority_node_id": "bacloud",
            "authority_epoch": 2,
            "node_attachment_id": "att-1",
            "node_instance_id": "firstbyte",
            "scope": "run",
            "hard_expires_at": future,
            "permit_expires_at": permit_expiry,
            "signature": "sig",
            "issued_at": utc_text(now),
        },
    )
    claimed = await repo.claim_next(1)
    assert claimed is not None
    assert claimed.cmd_hash == command.cmd_hash
    assert claimed.status == "running"


@pytest.mark.asyncio
async def test_revoked_remote_permit_never_reaches_running(tmp_path):
    repo, store = await repo_and_store(tmp_path)
    now = utc_now()
    command = await repo.create(
        "printf revoked",
        queue_id=1,
        agent_id="logical-remote",
        logical_agent_id="logical-remote",
        work_session_id="ws-revoked",
        session_epoch=9,
        command_type="persistent_run",
    )
    await store.record_command_permit(
        command.cmd_hash,
        {
            "logical_agent_id": "logical-remote",
            "work_session_id": "ws-revoked",
            "session_epoch": 9,
            "authority_node_id": "bacloud",
            "authority_epoch": 2,
            "node_attachment_id": "att-3",
            "node_instance_id": "firstbyte",
            "scope": "run",
            "hard_expires_at": utc_text(now + timedelta(minutes=5)),
            "permit_expires_at": utc_text(now + timedelta(seconds=30)),
            "signature": "sig",
            "issued_at": utc_text(now),
        },
    )
    await store.revoke_command_permits("logical-remote", "ws-revoked", 9)
    assert await repo.claim_next(1) is None
    row = await repo.get(command.cmd_hash)
    assert row.status == "cancelled"


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


def _persistent_admission(principal_id: str = "client-1"):
    return VerifiedAdmissionContext(
        principal_id=principal_id,
        credential_id=f"oauth:{principal_id}",
        scopes=frozenset({"terminal:read", "terminal:execute"}),
        auth_generation=1,
        transport="mcp",
        auth_mode="oauth",
    )
