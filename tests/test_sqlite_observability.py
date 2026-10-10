import asyncio
import sqlite3
from types import SimpleNamespace

import aiosqlite
import pytest

from terminal_mcp.observability import EventLogger
from terminal_mcp.runtime import RuntimeConfigProvider
from terminal_mcp.storage.sqlite_observability import (
    SqliteDiagnostics,
    SqliteMainFileError,
    cancellation_safe_connection,
    open_observed_connection,
)


class FakeEvents:
    def __init__(self):
        self.records = []

    def emit(self, event, level="INFO", **fields):
        self.records.append((event, level, fields))


class FakeMetrics:
    def __init__(self):
        self.increments = []
        self.values = {}

    def inc(self, name, labels=(), value=1):
        self.increments.append((name, tuple(labels), value))

    def set(self, name, value, labels=()):
        self.values[(name, tuple(labels))] = value


class FakeIoError(sqlite3.OperationalError):
    sqlite_errorcode = 778
    sqlite_errorname = "SQLITE_IOERR_WRITE"


def test_sqlite_diagnostics_preserve_extended_code_without_sensitive_message():
    events = FakeEvents()
    metrics = FakeMetrics()
    diagnostics = SqliteDiagnostics("durable", events, metrics)
    exc = FakeIoError("disk I/O error while writing secret-command-body token=abc")

    diagnostics.record(
        exc,
        operation="finish_command",
        stage="commit",
        command_hash="deadbeef",
        execution_outcome="completed",
        durable_finalization_outcome="failed",
    )

    event, level, fields = events.records[0]
    assert event == "sqlite_error"
    assert level == "ERROR"
    assert fields["sqlite_errorcode"] == 778
    assert fields["sqlite_errorname"] == "SQLITE_IOERR_WRITE"
    assert fields["sqlite_error_kind"] == "IOERR"
    assert fields["stage"] == "commit"
    assert fields["database_role"] == "durable"
    assert fields["command_hash"] == "deadbeef"
    assert fields["execution_outcome"] == "completed"
    assert fields["durable_finalization_outcome"] == "failed"
    assert fields["message"] == "database I/O error"
    rendered = repr(fields)
    assert "secret-command-body" not in rendered
    assert "token=abc" not in rendered
    assert any(
        name == "terminal_mcp_sqlite_errors_total"
        and ("kind", "IOERR") in labels
        and ("code", "778") in labels
        for name, labels, _ in metrics.increments
    )


def test_sqlite_diagnostics_runtime_switch_defaults_on_and_can_disable(tmp_path):
    assert RuntimeConfigProvider._parse("").sqlite_diagnostics is True
    disabled = RuntimeConfigProvider._parse("TERMINAL_MCP_SQLITE_DIAGNOSTICS=false\n")
    assert disabled.sqlite_diagnostics is False

    metrics = FakeMetrics()
    provider = SimpleNamespace(current=disabled)
    logger = EventLogger(tmp_path / "events.log", provider, metrics)
    logger.emit("sqlite_error", level="ERROR", sqlite_errorcode=10)
    assert logger.queue.empty()

    provider.current = RuntimeConfigProvider._parse("TERMINAL_MCP_SQLITE_DIAGNOSTICS=true\n")
    logger.emit("sqlite_error", level="ERROR", sqlite_errorcode=10)
    record = logger.queue.get_nowait()
    assert record.event["event"] == "sqlite_error"
    assert record.event["sqlite_errorcode"] == 10


def test_sqlite_diagnostics_never_emit_sql_or_parameter_fields():
    events = FakeEvents()
    diagnostics = SqliteDiagnostics("output", events, FakeMetrics())
    exc = FakeIoError("secret row value: hunter2")

    diagnostics.record(exc, operation="output_append", stage="execute", command_hash="cafebabe")

    fields = events.records[0][2]
    assert set(fields).isdisjoint({"sql", "parameters", "cmd", "command", "body"})
    assert "hunter2" not in repr(fields)


@pytest.mark.asyncio
async def test_cancelled_connection_open_is_closed_before_cancellation_propagates():
    started = asyncio.Event()
    release = asyncio.Event()

    class RawConnection:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    raw = RawConnection()

    async def connect(path, **kwargs):
        assert path == "db.sqlite3"
        assert kwargs["timeout"] == 1.0
        started.set()
        await release.wait()
        return raw

    task = asyncio.create_task(
        open_observed_connection(
            connect,
            "db.sqlite3",
            busy_timeout=1.0,
            diagnostics=SqliteDiagnostics("test"),
            operation="connect-test",
        )
    )
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert raw.closed is True


@pytest.mark.asyncio
async def test_cancelled_pragma_setup_closes_connected_database_before_propagating():
    started = asyncio.Event()
    release = asyncio.Event()

    class RawConnection:
        def __init__(self):
            self.closed = False

        async def execute(self, sql):
            assert sql == "PRAGMA busy_timeout=1000"
            started.set()
            await release.wait()

        async def close(self):
            self.closed = True

    raw = RawConnection()

    async def connect(path, **kwargs):
        assert path == "db.sqlite3"
        assert kwargs["timeout"] == 1.0
        return raw

    task = asyncio.create_task(
        open_observed_connection(
            connect,
            "db.sqlite3",
            busy_timeout=1.0,
            diagnostics=SqliteDiagnostics("test"),
            operation="pragma-test",
            pragmas=("PRAGMA busy_timeout=1000",),
        )
    )
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert raw.closed is True


@pytest.mark.asyncio
async def test_cancellation_safe_connection_rejects_http_overwrite_before_open(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    path.write_bytes(b"HTTP/1.1 500 Internal Server Error\r\n\r\nInternal Server Error")

    with pytest.raises(SqliteMainFileError, match="sqlite_main_invalid_header"):
        async with cancellation_safe_connection(aiosqlite.connect, path):
            pytest.fail("malformed main file must be rejected through SQLite")


@pytest.mark.asyncio
async def test_cancellation_safe_connection_rejects_wal_at_main_path(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    path.write_bytes(bytes.fromhex("377f0682") + b"\x00" * 64)

    with pytest.raises(SqliteMainFileError, match="sqlite_main_invalid_header"):
        async with cancellation_safe_connection(aiosqlite.connect, path):
            pytest.fail("WAL data must not be accepted as a SQLite main database")


@pytest.mark.asyncio
async def test_cancellation_safe_connection_allows_empty_bootstrap_file(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    path.touch()

    async with cancellation_safe_connection(aiosqlite.connect, path) as db:
        await db.execute("CREATE TABLE bootstrap_ok(value INTEGER)")
        await db.commit()

    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)


@pytest.mark.asyncio
async def test_cancellation_safe_connection_validates_file_uri(tmp_path):
    path = tmp_path / "runtime uri.sqlite3"
    path.write_bytes(b"HTTP/1.1 500 Internal Server Error\r\n\r\nInternal Server Error")
    uri = path.as_uri() + "?mode=ro"

    with pytest.raises(SqliteMainFileError, match="sqlite_main_invalid_header"):
        async with cancellation_safe_connection(aiosqlite.connect, uri, uri=True):
            pytest.fail("malformed file URI must be rejected before SQLite opens it")
