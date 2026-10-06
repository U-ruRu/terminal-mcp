"""Real application-controlled transactions over the existing authoritative schema.

This adapter deliberately does not wrap legacy stores: their independent connections
and commits cannot be enlisted atomically. Bound repositories below share exactly one
connection and leave commit/rollback exclusively to the unit of work. Output storage,
network calls and process execution are outside this boundary.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from os import PathLike
from pathlib import Path
from typing import Unpack

import aiosqlite

from terminal_mcp.application.ports import (
    ApplicationRepositories,
    CommandSnapshot,
    ContextEntry,
    ContextPatch,
    JsonValue,
    TaskSnapshot,
)
from terminal_mcp.core.persistent_agents import WorkSessionRecord
from terminal_mcp.storage.context import ContextStore

_active_transaction: ContextVar[bool] = ContextVar("application_transaction_active", default=False)


class ApplicationTransactionError(RuntimeError):
    """An invalid transaction scope, independent of HTTP/MCP error rendering."""


async def _finish_cleanup(operation: Awaitable[None]) -> None:
    """Drain rollback/close even when a caller cancels repeatedly."""
    pending = asyncio.ensure_future(operation)
    cancelled = False
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            cancelled = True
    pending.result()
    if cancelled:
        raise asyncio.CancelledError


async def _open_connection(path: Path, busy_timeout: float) -> aiosqlite.Connection:
    """Keep ownership of an in-flight open until it can be closed on cancellation."""
    opening = asyncio.ensure_future(aiosqlite.connect(path, timeout=busy_timeout))
    try:
        return await asyncio.shield(opening)
    except BaseException:

        async def discard_opened_connection() -> None:
            try:
                opened = await opening
            except BaseException:
                return
            await opened.close()

        await _finish_cleanup(discard_opened_connection())
        raise


class _Scope:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db
        self.owner = asyncio.current_task()
        self.active = True

    def check(self) -> None:
        if not self.active:
            raise ApplicationTransactionError("application transaction scope is closed")
        if asyncio.current_task() is not self.owner:
            raise ApplicationTransactionError("application transaction belongs to another task")

    async def fetchone(self, sql: str, parameters: tuple = ()):
        self.check()
        async with self.db.execute(sql, parameters) as cursor:
            return await cursor.fetchone()

    async def insert(self, sql: str, parameters: tuple) -> int:
        self.check()
        async with self.db.execute(sql, parameters) as cursor:
            assert cursor.lastrowid is not None
            return int(cursor.lastrowid)

    async def change(self, sql: str, parameters: tuple) -> int:
        self.check()
        async with self.db.execute(sql, parameters) as cursor:
            return cursor.rowcount


def _json(payload: Mapping[str, JsonValue] | None) -> str:
    return json.dumps(
        dict(payload or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


class _ContextRepository:
    def __init__(self, scope: _Scope):
        self._scope = scope

    @staticmethod
    def _entry(row) -> ContextEntry | None:
        if row is None:
            return None
        entry = ContextEntry(id=int(row[0]), summary=row[1], content=row[2], primary=bool(row[3]))
        if row[4] is not None:
            entry["namespace"] = row[4]
        return entry

    async def get(self, context_id: int, *, namespace: str | None = None) -> ContextEntry | None:
        if namespace is None:
            row = await self._scope.fetchone(
                "SELECT id,summary,content,is_primary,namespace FROM instance_context "
                "WHERE id=? AND namespace IS NULL",
                (int(context_id),),
            )
        else:
            row = await self._scope.fetchone(
                "SELECT id,summary,content,is_primary,namespace FROM instance_context "
                "WHERE id=? AND namespace=?",
                (int(context_id), namespace),
            )
        return self._entry(row)

    async def create(
        self, summary: str, content: str, primary: bool, *, namespace: str | None = None
    ) -> ContextEntry:
        self._scope.check()
        values = (
            ContextStore._summary(summary),
            ContextStore._content(content),
            int(ContextStore._primary(primary)),
            namespace,
        )
        context_id = await self._scope.insert(
            "INSERT INTO instance_context(summary,content,is_primary,namespace) VALUES(?,?,?,?)",
            values,
        )
        entry = ContextEntry(
            id=context_id, summary=values[0], content=values[1], primary=bool(values[2])
        )
        if namespace is not None:
            entry["namespace"] = namespace
        return entry

    async def update(
        self,
        context_id: int,
        *,
        namespace: str | None = None,
        **patch: Unpack[ContextPatch],
    ) -> ContextEntry | None:
        self._scope.check()
        if not patch:
            raise ValueError("at least one context field is required")
        unknown = patch.keys() - {"summary", "content", "primary"}
        if unknown:
            raise ValueError("unsupported context field")
        fields, values = [], []
        if "summary" in patch:
            fields.append("summary=?")
            values.append(ContextStore._summary(patch["summary"]))
        if "content" in patch:
            fields.append("content=?")
            values.append(ContextStore._content(patch["content"]))
        if "primary" in patch:
            fields.append("is_primary=?")
            values.append(int(ContextStore._primary(patch["primary"])))
        if namespace is None:
            where = "id=? AND namespace IS NULL"
            parameters = (*values, int(context_id))
        else:
            where = "id=? AND namespace=?"
            parameters = (*values, int(context_id), namespace)
        changed = await self._scope.change(
            f"UPDATE instance_context SET {','.join(fields)} WHERE {where}", parameters
        )
        return await self.get(context_id, namespace=namespace) if changed == 1 else None

    async def delete(self, context_id: int, *, namespace: str | None = None) -> bool:
        if namespace is None:
            sql = "DELETE FROM instance_context WHERE id=? AND namespace IS NULL"
            parameters = (int(context_id),)
        else:
            sql = "DELETE FROM instance_context WHERE id=? AND namespace=?"
            parameters = (int(context_id), namespace)
        return await self._scope.change(sql, parameters) == 1


class _SessionRepository:
    def __init__(self, scope: _Scope):
        self._scope = scope

    async def get_work_session(self, work_session_id: str) -> WorkSessionRecord | None:
        row = await self._scope.fetchone(
            "SELECT work_session_id,logical_agent_id,session_epoch,authority_node_id,"
            "authority_epoch,started_at,hard_expires_at,auth_principal_id,auth_generation,"
            "state,origin_instance_id,ended_at,end_reason "
            "FROM logical_agent_work_sessions WHERE work_session_id=?",
            (work_session_id,),
        )
        return WorkSessionRecord(*row) if row is not None else None

    async def activity(
        self, agent_id: str, tool: str, now: str, command_hash: str | None = None
    ) -> None:
        await self._scope.insert(
            "INSERT INTO agent_activity_events(agent_id,timestamp,tool,command_hash) "
            "VALUES(?,?,?,?)",
            (agent_id, now, tool, command_hash),
        )

    async def add_audit_event(
        self,
        logical_agent_id: str,
        event_type: str,
        *,
        principal_id: str,
        now: str,
        work_session_id: str | None = None,
        session_epoch: int | None = None,
        payload: Mapping[str, JsonValue] | None = None,
    ) -> int:
        return await self._scope.insert(
            "INSERT INTO persistent_agent_audit("
            "logical_agent_id,event_type,principal_id,work_session_id,session_epoch,payload_json,"
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (
                logical_agent_id,
                event_type,
                principal_id,
                work_session_id,
                session_epoch,
                _json(payload),
                now,
            ),
        )


class _TaskRepository:
    def __init__(self, scope: _Scope):
        self._scope = scope

    async def get_snapshot(self, namespace: str, task_id: str) -> TaskSnapshot | None:
        row = await self._scope.fetchone(
            "SELECT namespace,task_id,title,state,revision FROM work_items "
            "WHERE namespace=? AND task_id=?",
            (namespace, task_id),
        )
        return TaskSnapshot(*row) if row is not None else None

    async def add_event(
        self,
        namespace: str,
        task_id: str,
        event_type: str,
        *,
        now: str,
        agent_id: str | None = None,
        payload: Mapping[str, JsonValue] | None = None,
        logical_agent_id: str | None = None,
        work_session_id: str | None = None,
        session_epoch: int | None = None,
    ) -> int:
        return await self._scope.insert(
            "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at,"
            "logical_agent_id,work_session_id,session_epoch) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                namespace,
                task_id,
                event_type,
                agent_id,
                _json(payload),
                now,
                logical_agent_id,
                work_session_id,
                session_epoch,
            ),
        )


class _CommandRepository:
    def __init__(self, scope: _Scope):
        self._scope = scope

    async def get_snapshot(self, command_hash: str) -> CommandSnapshot | None:
        row = await self._scope.fetchone(
            "SELECT hash,status,queue_id,queue_sequence FROM commands WHERE hash=?", (command_hash,)
        )
        return CommandSnapshot(*row) if row is not None else None

    async def record_attribution(
        self,
        command_hash: str,
        agent_id: str,
        *,
        now: str,
        command_type: str,
        command_preview: str,
        logical_agent_id: str | None = None,
        work_session_id: str | None = None,
        session_epoch: int | None = None,
    ) -> None:
        if await self.get_snapshot(command_hash) is None:
            raise LookupError("command does not exist")
        await self._scope.insert(
            "INSERT INTO command_agent_attribution(command_hash,agent_id,created_at,command_type,"
            "command_preview,logical_agent_id,work_session_id,session_epoch) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                command_hash,
                agent_id,
                now,
                command_type,
                command_preview,
                logical_agent_id,
                work_session_id,
                session_epoch,
            ),
        )


class SqliteApplicationUnitOfWork:
    """One explicit transaction in an already initialized authoritative database.

    A successful scope commits once; a body exception/cancellation rolls back all
    repository mutations. Cancellation after COMMIT is submitted may observe a
    committed transaction, as with any durable commit; it must not be retried blindly.
    Never invoke independent legacy store mutations from inside this scope.
    """

    def __init__(self, path: str | PathLike[str], *, busy_timeout: float = 1.0):
        if str(path) == ":memory:" or str(path).startswith("file:"):
            raise ValueError("application unit of work requires an authoritative database path")
        if not math.isfinite(busy_timeout) or not 0 < busy_timeout <= 5:
            raise ValueError("busy_timeout must be greater than zero and at most five seconds")
        self.path = Path(path).resolve()
        self.busy_timeout = busy_timeout

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[ApplicationRepositories]:
        if _active_transaction.get():
            raise ApplicationTransactionError("nested application transactions are not supported")
        token = _active_transaction.set(True)
        db = None
        scope = None
        try:
            db = await _open_connection(self.path, self.busy_timeout)
            async with db.execute("PRAGMA foreign_keys=ON"):
                pass
            async with db.execute("BEGIN IMMEDIATE"):
                pass
            scope = _Scope(db)
            repositories = ApplicationRepositories(
                context=_ContextRepository(scope),
                sessions=_SessionRepository(scope),
                tasks=_TaskRepository(scope),
                commands=_CommandRepository(scope),
            )
            try:
                yield repositories
                scope.active = False
                await db.commit()
            except BaseException:
                scope.active = False
                await _finish_cleanup(db.rollback())
                raise
        finally:
            if scope is not None:
                scope.active = False
            try:
                if db is not None:
                    await _finish_cleanup(db.close())
            finally:
                _active_transaction.reset(token)
