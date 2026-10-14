"""Atomic live standalone PersistentBackend message routing check."""

import pytest

from terminal_mcp.auth.foundation import AuthFoundationStore
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


def admission():
    return VerifiedAdmissionContext(
        principal_id="standalone-client",
        credential_id="oauth:standalone-client",
        scopes=frozenset({"terminal:read", "terminal:execute"}),
        auth_generation=1,
        transport="mcp",
        auth_mode="oauth",
    )


async def standalone_fixture(tmp_path):
    repo = SqliteRepository(tmp_path / "runtime.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000, persistent_agents_enabled=True)
    store = PersistentAgentStore(repo.path)
    lifecycle = PersistentLifecycleCoordinator(
        store,
        enabled=True,
        authority_node_id="standalone",
        session_duration_seconds=180,
        execution_fence=PersistentExecutionFence(repo, terminal, service.task_store),
    )
    auth = AuthFoundationStore(tmp_path / "auth.sqlite3")
    await auth.initialize()
    backend = PersistentBackend(service, lifecycle, access_authority=auth)
    ctx = admission()

    async def create_agent(name):
        created = await lifecycle.create_slot(name, admission=ctx)
        logical_agent_id = created["slot"]["logical_agent_id"]
        access = await backend._access_ensure(logical_agent_id, display_suffix=name)
        armed = await lifecycle.play(
            logical_agent_id,
            expected_revision=created["slot"]["slot_revision"],
            admission=ctx,
        )
        started = await lifecycle.session_start(
            created["selector"]["selector"],
            expected_revision=armed["slot"]["slot_revision"],
            admission=ctx,
        )
        return {
            "logical_agent_id": logical_agent_id,
            "public_name": access["public_name"],
            "code": access["access_code"],
            "session": started["work_session"],
        }

    return backend, lifecycle, await create_agent("Sender"), await create_agent("Recipient"), ctx


@pytest.mark.asyncio
async def test_standalone_persistent_backend_delivers_direct_notify(tmp_path):
    backend, _, sender, recipient, admission_context = await standalone_fixture(tmp_path)
    token = bind_admission_context(admission_context)
    try:
        result = await backend.access_message(
            sender["public_name"],
            access_code=sender["code"],
            target=recipient["public_name"],
            text="standalone direct message",
            mode="notify",
        )
    finally:
        reset_admission_context(token)
    assert result["ok"] is True
    assert result["scope"] == "direct"
