import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

from terminal_mcp.config import Settings
from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_admission import (
    VerifiedAdmissionContext,
    bind_admission_context,
    reset_admission_context,
)
from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.core.persistent_execution import PersistentExecutionFence
from terminal_mcp.core.persistent_lifecycle import PersistentLifecycleCoordinator
from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.persistent_agents import PersistentAgentStore, PersistentStoreError
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


async def repo_and_store(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    return repo, PersistentAgentStore(repo.path)


@pytest.mark.asyncio
async def test_v16_tables_and_durable_idempotency(tmp_path):
    repo, store = await repo_and_store(tmp_path)
    await store.create_slot("logical-1", "Alpha", "A123", authority_node_id="node-a")

    import sqlite3

    with sqlite3.connect(repo.path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "persistent_idempotency",
        "persistent_agent_audit",
        "logical_agent_node_attachments",
        "persistent_command_permits",
    } <= tables

    request = {"expected_revision": 1}
    fingerprint = store.idempotency_fingerprint(request)
    result = {"ok": True, "slot_revision": 2}
    stored = await store.idempotency_put("logical-1", "play", "idem-0001", fingerprint, result)
    assert stored == result
    assert await store.idempotency_get("logical-1", "play", "idem-0001", fingerprint) == result
    # Same key + same payload is a replay, not another side effect.
    assert (
        await store.idempotency_put(
            "logical-1", "play", "idem-0001", fingerprint, {"ok": True, "slot_revision": 999}
        )
        == result
    )
    with pytest.raises(PersistentStoreError, match="idempotency_conflict"):
        await store.idempotency_get(
            "logical-1",
            "play",
            "idem-0001",
            store.idempotency_fingerprint({"expected_revision": 2}),
        )

    pending_key = "idem-pending-0001"
    assert await store.idempotency_reserve("logical-1", "suspend", pending_key, fingerprint) is None
    with pytest.raises(PersistentStoreError, match="idempotency_in_progress"):
        await store.idempotency_get("logical-1", "suspend", pending_key, fingerprint)
    with pytest.raises(PersistentStoreError, match="idempotency_in_progress"):
        await store.idempotency_reserve("logical-1", "suspend", pending_key, fingerprint)
    assert await store.idempotency_abort("logical-1", "suspend", pending_key, fingerprint)
    assert await store.idempotency_reserve("logical-1", "suspend", pending_key, fingerprint) is None
    completed = await store.idempotency_complete(
        "logical-1", "suspend", pending_key, fingerprint, result
    )
    assert completed == result


@pytest.mark.asyncio
async def test_remote_permit_is_accepted_only_while_exact_and_live(tmp_path):
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

    expired = await repo.create(
        "printf should-not-run",
        queue_id=2,
        agent_id="logical-remote",
        logical_agent_id="logical-remote",
        work_session_id="ws-expired",
        session_epoch=8,
        command_type="persistent_run",
    )
    await store.record_command_permit(
        expired.cmd_hash,
        {
            "logical_agent_id": "logical-remote",
            "work_session_id": "ws-expired",
            "session_epoch": 8,
            "authority_node_id": "bacloud",
            "authority_epoch": 2,
            "node_attachment_id": "att-2",
            "node_instance_id": "firstbyte",
            "scope": "run",
            "hard_expires_at": future,
            "permit_expires_at": utc_text(now - timedelta(seconds=1)),
            "signature": "sig",
            "issued_at": utc_text(now - timedelta(seconds=20)),
        },
    )
    assert await repo.claim_next(2) is None
    cancelled = await repo.get(expired.cmd_hash)
    assert cancelled.status == "cancelled"
    assert cancelled.error == "persistent.session_fenced"


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
    assert await store.revoke_command_permits("logical-remote", "ws-revoked", 9) == 1
    assert await repo.claim_next(1) is None
    row = await repo.get(command.cmd_hash)
    assert row.status == "cancelled"


@pytest.mark.asyncio
async def test_generic_cancel_cannot_bypass_persistent_work_session_fence(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
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


def test_persistent_session_thresholds_are_ordered():
    isolated_fleet = {
        "fleet_v1_source_enabled": False,
        "fleet_v1_authority_enabled": False,
        "fleet_v1_projection_enabled": False,
        "fleet_v1_public_enabled": False,
    }
    valid = Settings(
        **isolated_fleet,
        _env_file=None,
        persistent_agents_enabled=True,
        persistent_session_duration_sec=120,
        persistent_session_warning_after_sec=60,
        persistent_session_alert_after_sec=90,
    )
    assert valid.persistent_session_duration_sec == 120
    assert valid.persistent_session_rearm_after_sec == 180

    with pytest.raises(ValidationError, match="warning_after_sec"):
        Settings(
            **isolated_fleet,
            _env_file=None,
            persistent_agents_enabled=True,
            persistent_session_duration_sec=120,
            persistent_session_warning_after_sec=120,
            persistent_session_alert_after_sec=110,
        )
    disabled = Settings(
        **isolated_fleet,
        _env_file=None,
        persistent_agents_enabled=False,
        persistent_session_duration_sec=1,
        persistent_session_warning_after_sec=99,
        persistent_session_alert_after_sec=99,
    )
    assert disabled.persistent_agents_enabled is False

    with pytest.raises(ValidationError, match="alert_after_sec"):
        Settings(
            **isolated_fleet,
            _env_file=None,
            persistent_agents_enabled=True,
            persistent_session_duration_sec=120,
            persistent_session_warning_after_sec=60,
            persistent_session_alert_after_sec=60,
        )

    with pytest.raises(ValidationError, match="rearm_after_sec"):
        Settings(
            **isolated_fleet,
            _env_file=None,
            persistent_agents_enabled=True,
            persistent_session_duration_sec=120,
            persistent_session_warning_after_sec=60,
            persistent_session_alert_after_sec=90,
            persistent_session_rearm_after_sec=0,
        )


@pytest.mark.asyncio
async def test_persistent_fence_marks_claimed_pidless_for_pre_spawn_cancel(tmp_path):
    repo = SqliteRepository(tmp_path / "claimed.sqlite3", tmp_path / "claimed-output.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    command = await repo.create(
        "printf must-not-run > should-not-exist",
        status="running",
        queue_id=1,
        agent_id="logical-1",
        logical_agent_id="logical-1",
        work_session_id="ws-1",
        session_epoch=1,
        command_type="persistent_run",
    )
    terminal._mark_claimed(command.cmd_hash)
    fence = PersistentExecutionFence(repo, terminal, None)

    blockers = await fence.revoke_session("logical-1", "ws-1", 1, reason="suspend")
    assert blockers[0]["kind"] == "starting_command"
    assert command.cmd_hash in terminal.cancel_requested
    assert (await repo.get(command.cmd_hash)).status == "running"

    await terminal._execute(command, method="run")
    current = await repo.get(command.cmd_hash)
    assert current.status == "cancelled"
    assert not (tmp_path / "should-not-exist").exists()
    terminal._release_claimed(command.cmd_hash)


class _RemoteOnlyLifecycle:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def operation_guard(self, logical_agent_id):
        yield


class _RemotePermitBackend(PersistentBackend):
    def __init__(self):
        self.lifecycle = _RemoteOnlyLifecycle()

    async def _execution_authority(self, *args, **kwargs):
        return None, object()


@pytest.mark.asyncio
async def test_remote_persistent_run_rejects_task_scope_without_distributed_claim_fence():
    backend = _RemotePermitBackend()
    result = await backend.run(
        "true",
        logical_agent_id="logical-remote",
        work_session_id="ws-remote",
        session_epoch=7,
        queue_id=1,
        task_scope="all",
    )
    assert result == {"ok": False, "code": "policy_incompatible", "error": "policy_incompatible"}


@pytest.mark.asyncio
async def test_persistent_fence_observes_session_blockers_without_cancelling(tmp_path):
    repo = SqliteRepository(tmp_path / "drain.sqlite3", tmp_path / "drain-output.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
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


def _persistent_admission():
    return VerifiedAdmissionContext(
        principal_id="client-1",
        credential_id="oauth:client-1",
        scopes=frozenset({"terminal:read", "terminal:execute"}),
        auth_generation=1,
        transport="mcp",
        auth_mode="oauth",
    )


@pytest.mark.asyncio
async def test_persistent_recovery_has_exact_session_attribution_and_is_fenced(tmp_path):
    repo = SqliteRepository(tmp_path / "recovery.sqlite3", tmp_path / "recovery-output.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000, persistent_agents_enabled=True)
    store = PersistentAgentStore(repo.path)
    fence = PersistentExecutionFence(repo, terminal, service.task_store)
    lifecycle = PersistentLifecycleCoordinator(
        store,
        enabled=True,
        authority_node_id="node-a",
        session_duration_seconds=90,
        execution_fence=fence,
    )
    backend = PersistentBackend(service, lifecycle)
    ctx = _persistent_admission()
    created = await lifecycle.create_slot("Alpha", admission=ctx)
    logical_agent_id = created["slot"]["logical_agent_id"]
    armed = await lifecycle.play(logical_agent_id, expected_revision=1, admission=ctx)
    started = await lifecycle.session_start(
        created["selector"]["selector"],
        expected_revision=armed["slot"]["slot_revision"],
        admission=ctx,
    )
    session = started["work_session"]

    token = bind_admission_context(ctx)
    try:
        result = await backend.recovery(
            "printf recovery-ok",
            logical_agent_id=logical_agent_id,
            work_session_id=session["work_session_id"],
            session_epoch=session["session_epoch"],
        )
    finally:
        reset_admission_context(token)
    assert result["ok"] is True
    assert any("recovery-ok" in line for line in result["lines"])
    attribution = await repo.persistent_attribution(result["cmd_hash"])
    assert attribution == {
        "logical_agent_id": logical_agent_id,
        "work_session_id": session["work_session_id"],
        "session_epoch": session["session_epoch"],
    }
    await lifecycle.session_end(
        logical_agent_id,
        session["work_session_id"],
        session["session_epoch"],
        admission=ctx,
    )
    token = bind_admission_context(ctx)
    try:
        fenced = await backend.recovery(
            "printf must-not-run",
            logical_agent_id=logical_agent_id,
            work_session_id=session["work_session_id"],
            session_epoch=session["session_epoch"],
        )
    finally:
        reset_admission_context(token)
    assert fenced["ok"] is False
    assert fenced["code"] == "session_not_active"


@pytest.mark.asyncio
async def test_access_session_start_accepts_fresh_lifecycle_mapping(tmp_path):
    repo = SqliteRepository(
        tmp_path / "access-start.sqlite3", tmp_path / "access-start-output.sqlite3"
    )
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000, persistent_agents_enabled=True)
    store = PersistentAgentStore(repo.path)
    fence = PersistentExecutionFence(repo, terminal, service.task_store)
    lifecycle = PersistentLifecycleCoordinator(
        store,
        enabled=True,
        authority_node_id="node-a",
        session_duration_seconds=90,
        execution_fence=fence,
    )
    backend = PersistentBackend(service, lifecycle)
    ctx = _persistent_admission()
    created = await lifecycle.create_slot("Alpha", admission=ctx)
    logical_agent_id = created["slot"]["logical_agent_id"]
    await lifecycle.play(logical_agent_id, expected_revision=1, admission=ctx)

    async def resolve_access(_code):
        return {
            "logical_agent_id": logical_agent_id,
            "authority_node_id": "node-a",
            "slot_kind": "persistent",
            "public_name": "Alpha",
            "display_suffix": None,
        }

    backend._resolve_access = resolve_access
    token = bind_admission_context(ctx)
    try:
        result = await backend.access_session_start(mode="persistent", access_code="ABCD")
    finally:
        reset_admission_context(token)

    assert result["ok"] is True
    assert result["mode"] == "persistent"
    assert result["public_name"] == "Alpha"
    assert result["session_ref"].startswith("ws_")
    assert result["session_epoch"] == 1
    assert result["hard_expires_at"]


@pytest.mark.asyncio
async def test_access_end_serializes_with_run_materialization(tmp_path):
    repo = SqliteRepository(tmp_path / "stop-race.sqlite3", tmp_path / "stop-race-output.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000, persistent_agents_enabled=True)
    store = PersistentAgentStore(repo.path)
    fence = PersistentExecutionFence(repo, terminal, service.task_store)
    lifecycle = PersistentLifecycleCoordinator(
        store,
        enabled=True,
        authority_node_id="node-a",
        session_duration_seconds=90,
        execution_fence=fence,
    )
    backend = PersistentBackend(service, lifecycle)
    ctx = _persistent_admission()
    created = await lifecycle.create_slot("Alpha", admission=ctx)
    logical_agent_id = created["slot"]["logical_agent_id"]
    armed = await lifecycle.play(logical_agent_id, expected_revision=1, admission=ctx)
    started = await lifecycle.session_start(
        created["selector"]["selector"],
        expected_revision=armed["slot"]["slot_revision"],
        admission=ctx,
    )
    session = started["work_session"]

    async def resolve_access(_code):
        return {
            "logical_agent_id": logical_agent_id,
            "authority_node_id": "node-a",
            "slot_kind": "persistent",
            "public_name": "Alpha",
        }

    backend._resolve_access = resolve_access
    authorized = asyncio.Event()
    release_create = asyncio.Event()
    original_create = repo.create

    async def create_after_barrier(*args, **kwargs):
        authorized.set()
        await release_create.wait()
        return await original_create(*args, **kwargs)

    repo.create = create_after_barrier
    original_submit = terminal.submit

    async def hold_queued(_command):
        return None

    terminal.submit = hold_queued
    token = bind_admission_context(ctx)
    try:
        run_task = asyncio.create_task(
            backend.run(
                "printf race-ok",
                logical_agent_id=logical_agent_id,
                work_session_id=session["work_session_id"],
                session_epoch=session["session_epoch"],
                queue_id=1,
                task_scope="none",
            )
        )
        await authorized.wait()
        end_task = asyncio.create_task(backend.access_session_stop("ABCD"))
        await asyncio.sleep(0)
        assert not end_task.done()
        release_create.set()
        run_result = await run_task
        end_result = await end_task
    finally:
        reset_admission_context(token)
        repo.create = original_create
        terminal.submit = original_submit

    assert run_result["ok"] is True
    assert end_result["ok"] is True
    assert end_result["stopping"] is True
    assert {item["command_hash"] for item in end_result["blockers"]} == {run_result["cmd_hash"]}
    assert (await store.get_work_session(session["work_session_id"])).state == "stopping"
