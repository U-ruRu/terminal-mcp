"""Use a real initialized schema; no fake store or pretend cross-database atomicity."""

import asyncio
import sqlite3

import aiosqlite
import pytest
import pytest_asyncio

from terminal_mcp.application.ports import (
    ApplicationUnitOfWorkPort,
    CommandRepositoryPort,
    ContextRepositoryPort,
    SessionRepositoryPort,
    TaskRepositoryPort,
)
from terminal_mcp.core.persistent_agents import WorkSessionRecord
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.application_uow import (
    ApplicationTransactionError,
    SqliteApplicationUnitOfWork,
)
from terminal_mcp.storage.context import ContextStore
from terminal_mcp.storage.persistent_agents import PersistentAgentStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore

NOW = "2026-10-06T08:00:00.000Z"


@pytest_asyncio.fixture
async def repository(tmp_path):
    repo = SqliteRepository(tmp_path / "durable.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    return repo


def rows(path, sql, parameters=()):
    with sqlite3.connect(path) as db:
        return db.execute(sql, parameters).fetchall()


async def test_context_and_activity_commit_together_and_legacy_can_read(repository):
    uow = SqliteApplicationUnitOfWork(repository.path)
    assert isinstance(uow, ApplicationUnitOfWorkPort)
    async with uow.transaction() as tx:
        assert isinstance(tx.context, ContextRepositoryPort)
        assert isinstance(tx.sessions, SessionRepositoryPort)
        assert isinstance(tx.tasks, TaskRepositoryPort)
        assert isinstance(tx.commands, CommandRepositoryPort)
        entry = await tx.context.create("  Atomic context  ", "  Some content  ", True)
        await tx.sessions.activity("agent-uow", "context.create", NOW)
        assert await tx.context.get(entry["id"]) == entry
        # Independent connections see neither write before the single commit.
        assert await ContextStore(repository.path).get(entry["id"]) is None
        assert await AgentStore(repository.path).activity_count("agent-uow") == 0
    assert entry == {
        "id": 1,
        "summary": "Atomic context",
        "content": "Some content",
        "primary": True,
    }
    assert await ContextStore(repository.path).get(entry["id"]) == entry
    assert await AgentStore(repository.path).activity_count("agent-uow") == 1


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_all_repositories_roll_back_on_base_exception(repository, failure):
    uow = SqliteApplicationUnitOfWork(repository.path)
    with pytest.raises(failure):
        async with uow.transaction() as tx:
            await tx.context.create("Transient", "Must roll back", False)
            await tx.sessions.activity("agent-uow", "context.create", NOW)
            raise failure()
    assert await ContextStore(repository.path).list() == []
    assert await AgentStore(repository.path).activity_count("agent-uow") == 0
    async with uow.transaction() as tx:
        await tx.context.create("Next", "Connection is usable", False)
    assert len(await ContextStore(repository.path).list()) == 1


async def test_actual_task_cancellation_rolls_back_and_releases_writer(repository):
    entered = asyncio.Event()
    hold = asyncio.Event()
    uow = SqliteApplicationUnitOfWork(repository.path)

    async def mutation():
        async with uow.transaction() as tx:
            await tx.context.create("Cancelled", "Not durable", False)
            await tx.sessions.activity("cancelled-agent", "context.create", NOW)
            entered.set()
            await hold.wait()

    task = asyncio.create_task(mutation())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await ContextStore(repository.path).list() == []
    assert await AgentStore(repository.path).activity_count("cancelled-agent") == 0
    async with uow.transaction() as tx:
        await tx.context.create("After cancellation", "Writer released", False)


@pytest.mark.parametrize("other_path", [False, True])
async def test_nested_units_fail_before_open_or_lock_in_same_or_other_database(
    repository, tmp_path, other_path
):
    uow = SqliteApplicationUnitOfWork(repository.path)
    path = tmp_path / "different.sqlite3" if other_path else repository.path
    other = SqliteApplicationUnitOfWork(path)
    async with uow.transaction() as tx:
        async with asyncio.timeout(1):
            with pytest.raises(ApplicationTransactionError, match="nested"):
                async with other.transaction():
                    pytest.fail("nested transaction must not enter")
        await tx.context.create("Outer", "Remains usable", False)
    if other_path:
        assert not path.exists()
    assert len(await ContextStore(repository.path).list()) == 1


async def test_scoped_repositories_reject_child_task_and_lifetime_escape(repository):
    uow = SqliteApplicationUnitOfWork(repository.path)
    async with uow.transaction() as tx:
        task = asyncio.create_task(tx.context.create("Wrong owner", "No write", False))
        with pytest.raises(ApplicationTransactionError, match="another task"):
            await task

        async def nested_child():
            async with uow.transaction():
                pytest.fail("child must not start a nested transaction")

        with pytest.raises(ApplicationTransactionError, match="nested"):
            await asyncio.create_task(nested_child())
        await tx.context.create("Owner", "Valid write", False)
    with pytest.raises(ApplicationTransactionError, match="closed"):
        await tx.context.get(1)
    with pytest.raises(ApplicationTransactionError, match="closed"):
        await tx.sessions.activity("escaped", "context.read", NOW)
    assert len(await ContextStore(repository.path).list()) == 1


async def test_context_crud_parity_and_validation(repository):
    uow = SqliteApplicationUnitOfWork(repository.path)
    async with uow.transaction() as tx:
        with pytest.raises(ValueError, match="summary is required"):
            await tx.context.create("  ", "body", False)
        with pytest.raises(ValueError, match="at most 100"):
            await tx.context.create("x" * 101, "body", False)
        with pytest.raises(ValueError, match="content is required"):
            await tx.context.create("Summary", " ", False)
        with pytest.raises(ValueError, match="primary must be a boolean"):
            await tx.context.create("Summary", "body", 1)
        first = await tx.context.create("Original", "Content", False)
        changed = await tx.context.update(first["id"], summary="Changed", primary=True)
        assert changed == {**first, "summary": "Changed", "primary": True}
        with pytest.raises(ValueError, match="at least one"):
            await tx.context.update(first["id"])
        with pytest.raises(ValueError, match="unsupported"):
            await tx.context.update(first["id"], invalid="bad")
        assert await tx.context.update(9999, content="Missing") is None
        assert await tx.context.delete(first["id"])
        assert not await tx.context.delete(first["id"])
        assert await tx.context.get(first["id"]) is None
    assert await ContextStore(repository.path).list() == []


async def test_foreign_key_failure_rolls_back_other_repository(repository):
    uow = SqliteApplicationUnitOfWork(repository.path)
    with pytest.raises(sqlite3.IntegrityError):
        async with uow.transaction() as tx:
            await tx.context.create("Transient", "No partial commit", False)
            await tx.tasks.add_event("missing", "task", "test", now=NOW)
    assert await ContextStore(repository.path).list() == []
    assert rows(repository.path, "SELECT COUNT(*) FROM work_events")[0][0] == 0


async def test_task_event_command_attribution_and_audit_use_same_schema(repository):
    persistent = PersistentAgentStore(repository.path)
    await persistent.create_slot(
        "logical-uow", "Test identity", "2468", authority_node_id="local", now=NOW
    )
    session = WorkSessionRecord(
        work_session_id="session-uow",
        logical_agent_id="logical-uow",
        session_epoch=1,
        authority_node_id="local",
        authority_epoch=1,
        started_at=NOW,
        hard_expires_at="2026-10-06T08:23:00.000Z",
        auth_principal_id="principal-uow",
        auth_generation=1,
        state="active",
        origin_instance_id="local",
    )
    await persistent.record_work_session(session)
    await TaskStore(repository.path).create_task("uow", "task", "Test task", now=NOW)
    await repository.create("true", cmd_hash="abcd1234", queue_id=3)
    uow = SqliteApplicationUnitOfWork(repository.path)
    async with uow.transaction() as tx:
        task = await tx.tasks.get_snapshot("uow", "task")
        assert (task.namespace, task.task_id, task.title, task.state, task.revision) == (
            "uow",
            "task",
            "Test task",
            "ready",
            1,
        )
        command = await tx.commands.get_snapshot("abcd1234")
        assert (command.command_hash, command.status, command.queue_id) == (
            "abcd1234",
            "queued",
            3,
        )
        event_id = await tx.tasks.add_event(
            "uow",
            "task",
            "command",
            now=NOW,
            agent_id="agent-uow",
            payload={"command_hash": "abcd1234"},
            logical_agent_id="logical-uow",
            work_session_id="session-uow",
            session_epoch=1,
        )
        await tx.commands.record_attribution(
            "abcd1234",
            "agent-uow",
            now=NOW,
            command_type="run",
            command_preview="true",
            logical_agent_id="logical-uow",
            work_session_id="session-uow",
            session_epoch=1,
        )
        audit_id = await tx.sessions.add_audit_event(
            "logical-uow",
            "command.recorded",
            principal_id="principal-uow",
            now=NOW,
            work_session_id="session-uow",
            session_epoch=1,
            payload={"count": 1},
        )
        assert event_id > 0 and audit_id > 0
        assert await tx.tasks.get_snapshot("missing", "task") is None
        assert await tx.commands.get_snapshot("missing") is None
        assert await tx.sessions.get_work_session("missing") is None
        assert await tx.sessions.get_work_session("session-uow") == session
    assert await repository.persistent_attribution("abcd1234") == {
        "logical_agent_id": "logical-uow",
        "work_session_id": "session-uow",
        "session_epoch": 1,
    }
    assert rows(
        repository.path, "SELECT logical_agent_id FROM work_events WHERE id=?", (event_id,)
    ) == [
        ("logical-uow",),
    ]
    assert rows(
        repository.path, "SELECT event_type FROM persistent_agent_audit WHERE id=?", (audit_id,)
    ) == [
        ("command.recorded",),
    ]


async def test_command_attribution_cannot_create_orphan(repository):
    with pytest.raises(LookupError, match="does not exist"):
        async with SqliteApplicationUnitOfWork(repository.path).transaction() as tx:
            await tx.context.create("Transient", "Rollback", False)
            await tx.commands.record_attribution(
                "missing",
                "agent-uow",
                now=NOW,
                command_type="run",
                command_preview="true",
            )
    assert await ContextStore(repository.path).list() == []
    assert rows(repository.path, "SELECT COUNT(*) FROM command_agent_attribution")[0][0] == 0


async def test_concurrent_independent_transactions_commit_without_interleaving(repository):
    uow = SqliteApplicationUnitOfWork(repository.path)
    order = []

    async def write(name):
        async with uow.transaction() as tx:
            order.append((name, "start"))
            await tx.context.create(name, "Concurrent writer", False)
            await asyncio.sleep(0.02)
            await tx.sessions.activity(name, "context.create", NOW)
            order.append((name, "end"))

    await asyncio.gather(write("one"), write("two"))
    assert order[0][0] == order[1][0]
    assert order[2][0] == order[3][0]
    assert len(await ContextStore(repository.path).list()) == 2
    assert rows(repository.path, "SELECT COUNT(*) FROM agent_activity_events")[0][0] == 2


async def test_failed_open_does_not_poison_next_transaction(repository, tmp_path):
    invalid = SqliteApplicationUnitOfWork(tmp_path / "missing" / "db.sqlite3")
    with pytest.raises(sqlite3.OperationalError):
        async with invalid.transaction():
            pytest.fail("invalid path must fail")
    async with SqliteApplicationUnitOfWork(repository.path).transaction() as tx:
        await tx.context.create("Valid", "Works after failed open", False)


async def test_writer_busy_is_bounded_and_context_resets(repository):
    uow = SqliteApplicationUnitOfWork(repository.path, busy_timeout=0.03)
    with sqlite3.connect(repository.path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            async with asyncio.timeout(1):
                async with uow.transaction():
                    pytest.fail("writer already holds transaction")
        writer.rollback()
    async with uow.transaction() as tx:
        await tx.context.create("Recovered", "Writer released", False)


async def test_repeated_cancellation_drains_rollback(repository, monkeypatch):
    entered = asyncio.Event()
    rolling_back = asyncio.Event()
    release = asyncio.Event()
    original = aiosqlite.Connection.rollback

    async def delayed_rollback(db):
        rolling_back.set()
        await release.wait()
        await original(db)

    monkeypatch.setattr(aiosqlite.Connection, "rollback", delayed_rollback)

    async def mutation():
        async with SqliteApplicationUnitOfWork(repository.path).transaction() as tx:
            await tx.context.create("Cancelled twice", "Must not survive", False)
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(mutation())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    await asyncio.wait_for(rolling_back.wait(), 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await ContextStore(repository.path).list() == []
    async with SqliteApplicationUnitOfWork(repository.path).transaction() as tx:
        await tx.context.create("After cleanup", "No leaked writer", False)


async def test_close_finishes_when_caller_is_cancelled(repository, monkeypatch):
    closing = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()
    original = aiosqlite.Connection.close

    async def delayed_close(db):
        closing.set()
        await release.wait()
        await original(db)
        closed.set()

    monkeypatch.setattr(aiosqlite.Connection, "close", delayed_close)

    async def mutation():
        async with SqliteApplicationUnitOfWork(repository.path).transaction() as tx:
            await tx.context.create("Committed", "Commit precedes close", False)

    task = asyncio.create_task(mutation())
    await asyncio.wait_for(closing.wait(), 2)
    task.cancel()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    # Cancellation at close cannot undo an already completed commit.
    assert len(rows(repository.path, "SELECT id FROM instance_context")) == 1


@pytest.mark.parametrize("path", [":memory:", "file:test?mode=memory&cache=shared"])
def test_memory_and_uri_databases_are_not_an_authoritative_boundary(path):
    with pytest.raises(ValueError, match="authoritative database"):
        SqliteApplicationUnitOfWork(path)


@pytest.mark.parametrize("timeout", [0, -1, 6, float("inf"), float("nan")])
def test_busy_timeout_is_finite_and_bounded(tmp_path, timeout):
    with pytest.raises(ValueError, match="busy_timeout"):
        SqliteApplicationUnitOfWork(tmp_path / "db.sqlite3", busy_timeout=timeout)


async def test_cancellation_during_open_drains_and_closes_connection(repository, monkeypatch):
    opened = asyncio.Event()
    release = asyncio.Event()
    connections = []
    original = aiosqlite.connect

    async def delayed_connect(*args, **kwargs):
        db = await original(*args, **kwargs)
        connections.append(db)
        opened.set()
        await release.wait()
        return db

    monkeypatch.setattr(aiosqlite, "connect", delayed_connect)

    async def mutation():
        async with SqliteApplicationUnitOfWork(repository.path).transaction():
            pytest.fail("cancelled open must not enter application body")

    task = asyncio.create_task(mutation())
    await asyncio.wait_for(opened.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(connections) == 1
    with pytest.raises(ValueError, match="no active connection"):
        await connections[0].execute("SELECT 1")
    monkeypatch.setattr(aiosqlite, "connect", original)
    async with SqliteApplicationUnitOfWork(repository.path).transaction() as tx:
        await tx.context.create("Recovered", "Open ownership was drained", False)
