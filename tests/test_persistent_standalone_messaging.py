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
async def test_standalone_messaging_matches_fleet_delivery_semantics(tmp_path):
    backend, lifecycle, sender, recipient, ctx = await standalone_fixture(tmp_path)

    async def message(sender_name, **kwargs):
        token = bind_admission_context(ctx)
        try:
            return await backend.access_message(sender_name, **kwargs)
        finally:
            reset_admission_context(token)

    notify = await message(
        sender["public_name"],
        access_code=sender["code"],
        target=recipient["public_name"],
        text="standalone notify",
        mode="notify",
    )
    assert notify["ok"] is True, notify
    assert notify["scope"] == "direct"
    notify_hash = notify["message_hash"]

    for expected_seen in range(1, 6):
        state = await backend.message_state(
            logical_agent_id=recipient["logical_agent_id"],
            work_session_id=recipient["session"]["work_session_id"],
            session_epoch=recipient["session"]["session_epoch"],
            surface=True,
        )
        current = next(item for item in state["messages"] if item["message_hash"] == notify_hash)
        assert current["mode"] == "notify"
        assert current["sender"] == sender["public_name"]
        assert current["state"] == "read"
        assert current["seen_count"] == expected_seen
        assert state["ack_required_pending"] is False
        assert state["alert_pending"] is False

    expired = await backend.message_state(
        logical_agent_id=recipient["logical_agent_id"],
        work_session_id=recipient["session"]["work_session_id"],
        session_epoch=recipient["session"]["session_epoch"],
        surface=True,
    )
    assert all(item["message_hash"] != notify_hash for item in expired["messages"])

    ordinary_inbox = await message(
        recipient["public_name"],
        access_code=recipient["code"],
    )
    assert all(item["message_hash"] != notify_hash for item in ordinary_inbox["messages"])

    history = await message(
        recipient["public_name"],
        access_code=recipient["code"],
        show_all=True,
    )
    archived = next(item for item in history["messages"] if item["message_hash"] == notify_hash)
    assert archived["mode"] == "notify"
    assert archived["seen_count"] == 5

    ack = await message(
        sender["public_name"],
        access_code=sender["code"],
        target=recipient["public_name"],
        text="ack this",
        mode="ack",
    )
    ack_state = await backend.message_state(
        logical_agent_id=recipient["logical_agent_id"],
        work_session_id=recipient["session"]["work_session_id"],
        session_epoch=recipient["session"]["session_epoch"],
        surface=True,
    )
    assert ack_state["ack_required_pending"] is True
    assert ack_state["alert_pending"] is False
    acknowledged = await message(
        recipient["public_name"],
        access_code=recipient["code"],
        message_hash=ack["message_hash"],
    )
    assert acknowledged["ok"] is True
    cleared_ack = await backend.message_state(
        logical_agent_id=recipient["logical_agent_id"],
        work_session_id=recipient["session"]["work_session_id"],
        session_epoch=recipient["session"]["session_epoch"],
    )
    assert cleared_ack["ack_required_pending"] is False

    alert = await message(
        sender["public_name"],
        access_code=sender["code"],
        target=recipient["public_name"],
        text="reply before continuing",
        mode="alert",
    )
    alert_state = await backend.message_state(
        logical_agent_id=recipient["logical_agent_id"],
        work_session_id=recipient["session"]["work_session_id"],
        session_epoch=recipient["session"]["session_epoch"],
        surface=True,
    )
    assert alert_state["alert_pending"] is True

    ack_only = await message(
        recipient["public_name"],
        access_code=recipient["code"],
        message_hash=alert["message_hash"],
    )
    assert ack_only["ok"] is True
    still_alert = await backend.message_state(
        logical_agent_id=recipient["logical_agent_id"],
        work_session_id=recipient["session"]["work_session_id"],
        session_epoch=recipient["session"]["session_epoch"],
    )
    assert still_alert["alert_pending"] is True

    replied = await message(
        recipient["public_name"],
        access_code=recipient["code"],
        message_hash=alert["message_hash"],
        text="acknowledged and corrected",
    )
    assert replied["ok"] is True
    cleared_alert = await backend.message_state(
        logical_agent_id=recipient["logical_agent_id"],
        work_session_id=recipient["session"]["work_session_id"],
        session_epoch=recipient["session"]["session_epoch"],
    )
    assert cleared_alert["alert_pending"] is False

    broadcast = await message(
        sender["public_name"],
        access_code=sender["code"],
        target="broadcast",
        text="standalone broadcast",
        mode="notify",
    )
    assert broadcast["ok"] is True, broadcast
    assert broadcast["scope"] == "local"
    assert broadcast["delivered_to"] == [recipient["public_name"]]

    await lifecycle.session_end(
        recipient["logical_agent_id"],
        recipient["session"]["work_session_id"],
        recipient["session"]["session_epoch"],
        admission=ctx,
    )
    inactive = await message(
        sender["public_name"],
        access_code=sender["code"],
        target=recipient["public_name"],
        text="must not deliver",
        mode="notify",
    )
    assert inactive["code"] == "recipient_not_active"
