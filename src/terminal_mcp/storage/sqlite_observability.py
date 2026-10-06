from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager


def _error_kind(exc: sqlite3.Error) -> str:
    name = str(getattr(exc, "sqlite_errorname", "") or "").upper()
    message = str(exc).lower()
    if "BUSY" in name or "busy" in message:
        return "BUSY"
    if "LOCKED" in name or "locked" in message:
        return "LOCKED"
    if "IOERR" in name or "disk i/o" in message:
        return "IOERR"
    return "OTHER"


def _safe_message(kind: str) -> str:
    return {
        "BUSY": "database busy",
        "LOCKED": "database locked",
        "IOERR": "database I/O error",
    }.get(kind, "database operation failed")


def _stage_for_sql(sql: str) -> str:
    token = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if token == "PRAGMA":
        return "PRAGMA"
    if token in {"BEGIN", "SAVEPOINT"}:
        return "begin"
    if token in {"ROLLBACK", "RELEASE"}:
        return "rollback"
    if token in {"COMMIT", "END"}:
        return "commit"
    return "execute"


class SqliteDiagnostics:
    def __init__(self, role: str, events=None, metrics=None):
        self.role = role
        self.events = events
        self.metrics = metrics

    def configure(self, events, metrics) -> None:
        self.events = events
        self.metrics = metrics

    def record(
        self,
        exc: sqlite3.Error,
        *,
        operation: str,
        stage: str,
        command_hash: str | None = None,
        execution_outcome: str | None = None,
        durable_finalization_outcome: str | None = None,
        attempt: int = 1,
        retry_count: int = 0,
    ) -> str:
        if getattr(exc, "_terminal_mcp_sqlite_recorded", False):
            return _error_kind(exc)
        try:
            exc._terminal_mcp_sqlite_recorded = True
        except Exception:
            pass

        code = getattr(exc, "sqlite_errorcode", None)
        name = getattr(exc, "sqlite_errorname", None)
        kind = _error_kind(exc)
        fields = {
            "outcome": "error",
            "operation": operation,
            "stage": stage,
            "exception_class": type(exc).__name__,
            "sqlite_errorcode": code,
            "sqlite_errorname": name,
            "sqlite_error_kind": kind,
            "message": _safe_message(kind),
            "database_role": self.role,
            "journal_mode": "wal",
            "sqlite_version": sqlite3.sqlite_version,
            "attempt": attempt,
            "retry_count": retry_count,
        }
        if command_hash:
            fields["command_hash"] = command_hash
        if execution_outcome is not None:
            fields["execution_outcome"] = execution_outcome
        if durable_finalization_outcome is not None:
            fields["durable_finalization_outcome"] = durable_finalization_outcome

        labels = (
            ("kind", kind),
            ("code", str(code) if code is not None else "unknown"),
            ("stage", stage),
            ("role", self.role),
        )
        if self.metrics:
            self.metrics.inc("terminal_mcp_sqlite_errors_total", labels)
            if kind in {"BUSY", "LOCKED"}:
                self.metrics.inc("terminal_mcp_sqlite_busy_total")
        if self.events:
            event = "sqlite_busy" if kind in {"BUSY", "LOCKED"} else "sqlite_error"
            self.events.emit(
                event,
                level="WARNING" if event == "sqlite_busy" else "ERROR",
                **fields,
            )
        return kind

    def translate_busy(self, exc: sqlite3.Error, operation: str, timeout: float):
        if _error_kind(exc) in {"BUSY", "LOCKED"}:
            return RuntimeError(
                f"sqlite.{operation}: database busy after {round(timeout * 1000)} ms"
            )
        return None


class ObservedConnection:
    def __init__(
        self,
        db,
        diagnostics: SqliteDiagnostics,
        operation: str,
        *,
        command_hash: str | None = None,
        execution_outcome: str | None = None,
        durable_finalization_outcome: str | None = None,
    ):
        self._db = db
        self._diagnostics = diagnostics
        self._operation = operation
        self._context = {
            "command_hash": command_hash,
            "execution_outcome": execution_outcome,
            "durable_finalization_outcome": durable_finalization_outcome,
        }

    async def _call(self, stage, fn, *args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except sqlite3.Error as exc:
            self._diagnostics.record(exc, operation=self._operation, stage=stage, **self._context)
            raise

    async def execute(self, sql, parameters=None):
        args = (sql,) if parameters is None else (sql, parameters)
        return await self._call(_stage_for_sql(sql), self._db.execute, *args)

    async def executemany(self, sql, parameters):
        return await self._call(_stage_for_sql(sql), self._db.executemany, sql, parameters)

    async def executescript(self, sql):
        return await self._call("execute", self._db.executescript, sql)

    async def commit(self):
        return await self._call("commit", self._db.commit)

    async def rollback(self):
        return await self._call("rollback", self._db.rollback)

    async def close(self):
        return await self._call("close", self._db.close)

    def __getattr__(self, name):
        return getattr(self._db, name)


async def _open_cancellation_safe_connection(connect, path, **kwargs):
    connect_task = asyncio.ensure_future(connect(path, **kwargs))
    try:
        return await asyncio.shield(connect_task)
    except asyncio.CancelledError:
        # aiosqlite starts the worker thread before its initial connection future
        # resolves. Finish that handshake and close on the live loop before
        # propagating cancellation, otherwise the worker can outlive the loop.
        try:
            db = await connect_task
        except BaseException:
            pass
        else:
            await db.close()
        raise


@asynccontextmanager
async def cancellation_safe_connection(connect, path, **kwargs):
    db = await _open_cancellation_safe_connection(connect, path, **kwargs)
    try:
        yield db
    finally:
        await db.close()


async def open_observed_connection(
    connect,
    path,
    *,
    busy_timeout: float,
    diagnostics: SqliteDiagnostics,
    operation: str,
    pragmas: tuple[str, ...] = (),
    command_hash: str | None = None,
    execution_outcome: str | None = None,
    durable_finalization_outcome: str | None = None,
):
    try:
        raw = await _open_cancellation_safe_connection(connect, path, timeout=busy_timeout)
    except sqlite3.Error as exc:
        diagnostics.record(
            exc,
            operation=operation,
            stage="connect",
            command_hash=command_hash,
            execution_outcome=execution_outcome,
            durable_finalization_outcome=durable_finalization_outcome,
        )
        translated = diagnostics.translate_busy(exc, operation, busy_timeout)
        if translated:
            raise translated from exc
        raise

    db = ObservedConnection(
        raw,
        diagnostics,
        operation,
        command_hash=command_hash,
        execution_outcome=execution_outcome,
        durable_finalization_outcome=durable_finalization_outcome,
    )
    try:
        for pragma in pragmas:
            await db.execute(pragma)
    except BaseException as exc:
        await db.close()
        if isinstance(exc, sqlite3.Error):
            translated = diagnostics.translate_busy(exc, operation, busy_timeout)
            if translated:
                raise translated from exc
        raise
    return db


@asynccontextmanager
async def observed_connection(
    connect,
    path,
    *,
    busy_timeout: float,
    diagnostics: SqliteDiagnostics,
    operation: str,
    pragmas: tuple[str, ...] = (),
    command_hash: str | None = None,
    execution_outcome: str | None = None,
    durable_finalization_outcome: str | None = None,
):
    db = await open_observed_connection(
        connect,
        path,
        busy_timeout=busy_timeout,
        diagnostics=diagnostics,
        operation=operation,
        pragmas=pragmas,
        command_hash=command_hash,
        execution_outcome=execution_outcome,
        durable_finalization_outcome=durable_finalization_outcome,
    )
    try:
        yield db
    except sqlite3.Error as exc:
        diagnostics.record(
            exc,
            operation=operation,
            stage="execute",
            command_hash=command_hash,
            execution_outcome=execution_outcome,
            durable_finalization_outcome=durable_finalization_outcome,
        )
        translated = diagnostics.translate_busy(exc, operation, busy_timeout)
        if translated:
            raise translated from exc
        raise
    finally:
        await db.close()
