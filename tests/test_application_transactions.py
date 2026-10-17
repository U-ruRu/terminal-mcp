"""Real application entry points own their single-database transaction."""

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from terminal_mcp.application import ActorContext, TerminalApplication
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.application_uow import SqliteApplicationUnitOfWork
from terminal_mcp.storage.context import ContextStore
from terminal_mcp.storage.sqlite import SqliteRepository


class NativeIdentity:
    async def resolve(self, actor, operation):
        return {
            "ok": True,
            "logical_agent_id": "logical-owner",
            "work_session_id": "work",
            "session_epoch": 1,
            "authority_node_id": "authority",
            "issuer_node_id": "node",
            "slot_id": "native-slot",
        }


    @staticmethod
    def connection_key(actor):
        return "fixture-attachment"


async def application(tmp_path, *, failing=False):
    repository = SqliteRepository(tmp_path / "state.sqlite3", tmp_path / "output.sqlite3")
    await repository.initialize()
    uow = SqliteApplicationUnitOfWork(repository.path)
    if failing:

        class FailedActivity:
            async def activity(self, *args):
                raise RuntimeError("activity write failed")

        class FailingUow:
            @asynccontextmanager
            async def transaction(self):
                async with uow.transaction() as repositories:
                    yield replace(repositories, sessions=FailedActivity())

        selected = FailingUow()
    else:
        selected = uow
    service = SimpleNamespace(
        context_store=ContextStore(repository.path), access_mesh=NativeIdentity()
    )
    app = TerminalApplication(service, unit_of_work=selected)
    service.application = app
    return app, repository


async def test_context_create_records_resolved_actor_atomically(tmp_path):
    app, repository = await application(tmp_path)
    result = await app.contexts.mutate(
        ActorContext(node_id="node", logical_agent_id="logical-owner"),
        "create", summary="Shared", content="Content", primary=True,
    )
    assert result["entry"] == {
        "id": 1,
        "summary": "Shared",
        "content": "Content",
        "primary": True,
    }
    assert await AgentStore(repository.path).activity_count("logical-owner") == 1


@pytest.mark.asyncio
async def test_application_does_not_commit_context_when_activity_write_fails(tmp_path):
    app, repository = await application(tmp_path, failing=True)
    with pytest.raises(RuntimeError, match="activity write failed"):
        await app.contexts.mutate(
            ActorContext(logical_agent_id="logical-owner"),
            "create", summary="Transient", content="Roll back",
        )
    assert await app.service.context_store.list() == []
    assert await AgentStore(repository.path).activity_count("logical-owner") == 0
