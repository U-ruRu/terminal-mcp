"""Real application entry points own their single-database transaction."""

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from terminal_mcp.application import ActorContext, TerminalApplication
from terminal_mcp.application.requests import ContextCreateRequest, ContextDeleteRequest
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.application_uow import SqliteApplicationUnitOfWork
from terminal_mcp.storage.context import ContextStore
from terminal_mcp.storage.sqlite import SqliteRepository


class IdentityBackend:
    async def access_identity(self, code):
        assert code == "0042"
        return {
            "ok": True,
            "logical_agent_id": "logical-owner",
            "work_session_id": "work",
            "session_epoch": 1,
            "authority_node_id": "authority",
        }


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
        context_store=ContextStore(repository.path), persistent=IdentityBackend()
    )
    app = TerminalApplication(service, unit_of_work=selected)
    service.application = app
    return app, repository


@pytest.mark.asyncio
async def test_canonical_context_mutation_records_resolved_actor_in_same_transaction(tmp_path):
    app, repository = await application(tmp_path)
    actor = ActorContext(node_id="node")
    result = await app.context(
        actor,
        ContextCreateRequest(
            action="create",
            code="0042",
            summary=" Shared ",
            content=" Content ",
            primary=True,
        ),
    )
    assert result == {
        "ok": True,
        "entry": {
            "id": 1,
            "summary": "Shared",
            "content": "Content",
            "primary": True,
        },
    }
    assert await app.service.context_store.get(1) == result["entry"]
    assert await AgentStore(repository.path).activity_count("logical-owner") == 1
    assert actor.logical_agent_id is None
    assert await app.context(
        actor, ContextDeleteRequest(action="delete", code="0042", context_id=1)
    ) == {
        "ok": True,
        "deleted_id": 1,
    }
    assert await AgentStore(repository.path).activity_count("logical-owner") == 2


@pytest.mark.asyncio
async def test_application_does_not_commit_context_when_activity_write_fails(tmp_path):
    app, repository = await application(tmp_path, failing=True)
    with pytest.raises(RuntimeError, match="activity write failed"):
        await app.context(
            ActorContext(),
            ContextCreateRequest(
                action="create",
                code="0042",
                summary="Transient",
                content="Roll back",
            ),
        )
    assert await app.service.context_store.list() == []
    assert await AgentStore(repository.path).activity_count("logical-owner") == 0


@pytest.mark.asyncio
async def test_legacy_context_facade_uses_the_same_mutation_semantics(tmp_path):
    app, repository = await application(tmp_path)
    created = await app.compatibility.call(
        ActorContext(),
        "context",
        "create",
        summary=" Legacy ",
        content=" Shared ",
        primary=False,
    )
    assert created["entry"]["summary"] == "Legacy"
    assert await app.service.context_store.get(created["entry"]["id"]) == created["entry"]
    assert await AgentStore(repository.path).activity_count("logical-owner") == 0
    denied = await app.compatibility.call(
        ActorContext(endpoint_role="coordinator"),
        "run",
        "printf denied",
    )
    assert denied["code"] == "capability_not_allowed"


@pytest.mark.asyncio
@pytest.mark.parametrize("controls", [{"show_details": True}, {"limit": 1}, {"offset": 1}])
async def test_legacy_context_validation_does_not_drop_forbidden_read_controls(tmp_path, controls):
    app, _repository = await application(tmp_path)
    result = await app.compatibility.call(
        ActorContext(),
        "context",
        "create",
        summary="Reject",
        content="No write",
        primary=False,
        **controls,
    )
    assert result == {"ok": False, "error": "context.create: read controls are only valid for list"}
    assert await app.service.context_store.list() == []


@pytest.mark.asyncio
async def test_mutation_entry_cannot_accidentally_delete_on_list_action(tmp_path):
    app, _repository = await application(tmp_path)
    await app.context(
        ActorContext(),
        ContextCreateRequest(
            action="create",
            code="0042",
            summary="Keep",
            content="Still present",
        ),
    )
    assert not (await app.contexts.mutate(ActorContext(), "list", context_id=1))["ok"]
    assert await app.service.context_store.get(1) is not None
