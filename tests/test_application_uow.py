"""Use a real initialized schema; no fake store or pretend cross-database atomicity."""

import asyncio
import sqlite3

import pytest
import pytest_asyncio

from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.application_uow import (
    ApplicationTransactionError,
    SqliteApplicationUnitOfWork,
)
from terminal_mcp.storage.context import ContextStore
from terminal_mcp.storage.sqlite import SqliteRepository

NOW = "2026-10-06T08:00:00.000Z"


@pytest_asyncio.fixture
async def repository(tmp_path):
    repo = SqliteRepository(tmp_path / "durable.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    return repo


def rows(path, sql, parameters=()):
    with sqlite3.connect(path) as db:
        return db.execute(sql, parameters).fetchall()


async def test_transaction_failure_rolls_back_its_writes(repository):
    uow = SqliteApplicationUnitOfWork(repository.path)
    with pytest.raises(RuntimeError):
        async with uow.transaction() as tx:
            await tx.context.create("Transient", "Must roll back", False)
            await tx.sessions.activity("agent-uow", "context.create", NOW)
            raise RuntimeError()
    assert await ContextStore(repository.path).list() == []
    assert await AgentStore(repository.path).activity_count("agent-uow") == 0


async def test_cancelled_transaction_rolls_back_its_writes(repository):
    entered = asyncio.Event()
    uow = SqliteApplicationUnitOfWork(repository.path)

    async def mutation():
        async with uow.transaction() as tx:
            await tx.context.create("Cancelled", "Not durable", False)
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(mutation())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await ContextStore(repository.path).list() == []


async def test_transaction_rejects_repository_use_from_child_task(repository):
    async with SqliteApplicationUnitOfWork(repository.path).transaction() as tx:
        task = asyncio.create_task(tx.context.create("Wrong owner", "No write", False))
        with pytest.raises(ApplicationTransactionError, match="another task"):
            await task


async def test_concurrent_independent_transactions_commit_without_interleaving(repository):
    uow = SqliteApplicationUnitOfWork(repository.path)
    order = []

    async def write(name):
        async with uow.transaction() as tx:
            order.append((name, "start"))
            await tx.context.create(name, "Concurrent writer", False)
            await asyncio.sleep(0)
            await tx.sessions.activity(name, "context.create", NOW)
            order.append((name, "end"))

    await asyncio.gather(write("one"), write("two"))
    assert order[0][0] == order[1][0]
    assert order[2][0] == order[3][0]
    assert len(await ContextStore(repository.path).list()) == 2
    assert rows(repository.path, "SELECT COUNT(*) FROM agent_activity_events")[0][0] == 2
