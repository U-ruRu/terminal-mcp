"""Real bash recovery and its durable work-session attribution/fence boundary."""

import pytest

from terminal_mcp.core.persistent_admission import (
    VerifiedAdmissionContext,
    bind_admission_context,
    reset_admission_context,
)
from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.core.persistent_execution import PersistentExecutionFence
from terminal_mcp.core.persistent_lifecycle import PersistentLifecycleCoordinator
from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.persistent_agents import PersistentAgentStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


def _persistent_admission(principal_id: str = "client-1"):
    return VerifiedAdmissionContext(
        principal_id=principal_id,
        credential_id=f"oauth:{principal_id}",
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
