"""Standalone native Access delivers a direct message without remote transport."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from native_runtime_helpers import native_agent
from test_access_mesh_native_lifecycle import actor

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.application.access_mesh_messages import AccessMeshMessaging
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


async def test_standalone_persistent_backend_delivers_direct_notify(tmp_path):
    repo = SqliteRepository(tmp_path / "runtime.sqlite3")
    await repo.initialize()
    store, _ = native_agent(repo.path, "sender", code="1234")
    native_agent(repo.path, "recipient", code="5678")
    mesh = AccessMeshApplication(
        store,
        task_store=TaskStore(repo.path),
        execution_fence=SimpleNamespace(revoke_session=AsyncMock(return_value=[])),
    )
    sender, recipient = actor("coordinator", "sender"), actor("coordinator", "recipient")
    await mesh.attach(sender, session_number="1234")
    await mesh.attach(recipient, session_number="5678")
    messages = AccessMeshMessaging(mesh, AgentStore(repo.path))
    result = await messages.message(
        sender,
        target=store.human_name(store.slot_for_agent("recipient")),
        text="standalone direct message",
        mode="notify",
    )
    assert result["ok"] and result["scope"] == "local"
    inbox = await messages.message(replace(recipient, request_id="read"), history=True)
    assert any(m["text"] == "standalone direct message" for m in inbox["messages"])
