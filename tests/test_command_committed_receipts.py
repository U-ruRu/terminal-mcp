"""Committed commands keep their receipts when optional follow-up work fails."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import terminal_mcp.core.persistent_backend as backend_module
from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.commands import CommandApplication
from terminal_mcp.application.requests import CmdRunRequest
from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.core.persistent_lifecycle import PersistentLifecycleError
from terminal_mcp.mcp.output_contracts import cmd_result
from terminal_mcp.storage.sqlite import SqliteRepository

IDENTITY = {
    "logical_agent_id": "receipt-agent",
    "work_session_id": "receipt-session",
    "session_epoch": 1,
}


class _Lifecycle:
    exit_error = None

    @asynccontextmanager
    async def operation_guard(self, logical_agent_id):
        yield
        if self.exit_error is not None:
            raise self.exit_error


class _Gate:
    async def identity(self, actor, code, operation=None):
        return dict(IDENTITY), None

    async def command_state(self, actor, identity, action):
        return {}, None


async def _backend(tmp_path, monkeypatch):
    repo = SqliteRepository(tmp_path / "commands.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    terminal = SimpleNamespace(
        queue_workers=1,
        least_loaded_queue=AsyncMock(return_value=1),
        submit=AsyncMock(),
        enqueue_cancel=Mock(),
    )
    service = SimpleNamespace(
        repo=repo,
        terminal=terminal,
        task_coordinator=None,
        task_store=None,
        agent_policy=SimpleNamespace(command_preview_chars=160),
    )
    backend = PersistentBackend(service, _Lifecycle())
    backend._execution_authority = AsyncMock(return_value=(
        SimpleNamespace(hard_expires_at="2099-01-01T00:00:00Z", authority_node_id="node-a"),
        None,
    ))
    backend._task_refs = AsyncMock(return_value=[])
    service.persistent = backend
    monkeypatch.setattr(backend_module, "COMMAND_RUN_INLINE_BUDGET_SECONDS", 0)
    return backend


def _error(kind):
    if kind == "lifecycle":
        return PersistentLifecycleError("session_expired")
    return RuntimeError("injected optional follow-up failure")


def _command_count(repo):
    with sqlite3.connect(repo.path) as connection:
        return connection.execute("SELECT COUNT(*) FROM commands").fetchone()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["runtime", "lifecycle"])
@pytest.mark.parametrize("boundary", ["submit", "readback", "guard_exit"])
async def test_run_preserves_committed_hash_on_followup_failure(
    tmp_path, monkeypatch, kind, boundary
):
    backend = await _backend(tmp_path, monkeypatch)
    error = _error(kind)
    with monkeypatch.context() as patch:
        if boundary == "submit":
            backend.terminal.submit.side_effect = error
        elif boundary == "readback":
            patch.setattr(backend.repo, "get", AsyncMock(side_effect=error))
        else:
            backend.lifecycle.exit_error = error
        result = await backend.run("printf receipt", **IDENTITY, queue_id=1, task_scope="none")
    assert result["ok"] is True, result
    assert result["postcommit_warning"] == type(error).__name__
    stored = await backend.repo.get(result["cmd_hash"])
    assert stored is not None and stored.cmd == "printf receipt"
    assert _command_count(backend.repo) == 1
    backend.terminal.submit.assert_awaited_once()
    wire = cmd_result(result, "run")
    assert wire.structuredContent["ok"] is True
    assert wire.structuredContent["command"]["cmd_hash"] == stored.cmd_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["authority", "create"])
async def test_run_precommit_failure_has_no_command(tmp_path, monkeypatch, boundary):
    backend = await _backend(tmp_path, monkeypatch)
    if boundary == "authority":
        backend._execution_authority.side_effect = PersistentLifecycleError("session_expired")
    else:
        monkeypatch.setattr(backend.repo, "create", AsyncMock(side_effect=RuntimeError("write")))
    result = await backend.run("printf never", **IDENTITY, queue_id=1, task_scope="none")
    assert result["ok"] is False
    assert result["code"] == ("session_expired" if boundary == "authority" else "run_failed")
    assert "cmd_hash" not in result
    assert _command_count(backend.repo) == 0
    backend.terminal.submit.assert_not_awaited()


async def _attributed_command(backend, status="running", **identity):
    identity = {**IDENTITY, **identity}
    return await backend.repo.create(
        "printf cancel-receipt", status=status, queue_id=1,
        agent_id=identity["logical_agent_id"], command_type="persistent_run", **identity,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["runtime", "lifecycle"])
@pytest.mark.parametrize("boundary", ["enqueue", "guard_exit"])
async def test_cancel_preserves_durable_intent_on_followup_failure(
    tmp_path, monkeypatch, kind, boundary
):
    backend = await _backend(tmp_path, monkeypatch)
    command = await _attributed_command(backend)
    error = _error(kind)
    if boundary == "enqueue":
        backend.terminal.enqueue_cancel.side_effect = error
    else:
        backend.lifecycle.exit_error = error
    result = await backend.cancel(command.cmd_hash, **IDENTITY)
    assert result["ok"] is True, result
    assert result["postcommit_warning"] == type(error).__name__
    reopened = SqliteRepository(backend.repo.path, tmp_path / "output.sqlite3")
    pending = await reopened.active_cancel_intents()
    assert [item.cmd_hash for item in pending] == [command.cmd_hash]
    backend.terminal.enqueue_cancel.side_effect = None
    backend.lifecycle.exit_error = None
    retried = await backend.cancel(command.cmd_hash, **IDENTITY)
    assert retried["ok"] is True
    wire = cmd_result(result, "cancel")
    assert wire.structuredContent["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["missing", "foreign", "finished", "authority"])
async def test_cancel_rejects_before_accepting_intent(tmp_path, monkeypatch, scenario):
    backend = await _backend(tmp_path, monkeypatch)
    command = await _attributed_command(
        backend, status="completed" if scenario == "finished" else "running",
        logical_agent_id="another-agent" if scenario == "foreign" else IDENTITY["logical_agent_id"],
    )
    if scenario == "authority":
        backend._execution_authority.side_effect = PersistentLifecycleError("session_expired")
    target = "missing" if scenario == "missing" else command.cmd_hash
    result = await backend.cancel(target, **IDENTITY)
    assert result["ok"] is False
    assert result["code"] == {
        "missing": "command_not_found", "foreign": "command_not_owned",
        "finished": "command_already_finished", "authority": "session_expired",
    }[scenario]
    with sqlite3.connect(backend.repo.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM command_cancel_intents").fetchone()[0] == 0
    backend.terminal.enqueue_cancel.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_application_keeps_receipt_when_inline_stdout_read_fails(
    tmp_path, monkeypatch, status
):
    backend = await _backend(tmp_path, monkeypatch)

    async def finish(command):
        command.status = status
        command.exit_code = 0 if status == "completed" else 7
        await backend.repo.update(command)

    backend.terminal.submit.side_effect = finish
    backend.service.read = AsyncMock(side_effect=RuntimeError("output cache unavailable"))
    app = CommandApplication(backend.service, _Gate())
    result = await app.cmd(
        ActorContext(), CmdRunRequest(action="run", code="1234", command="printf done"),
    )
    assert result["ok"] is True, result
    assert result["status"] == status
    assert result["exit_code"] == (0 if status == "completed" else 7)
    assert "lines" not in result
    assert _command_count(backend.repo) == 1
    assert (await backend.repo.get(result["cmd_hash"])).status == status
    backend.terminal.submit.assert_awaited_once()
    wire = cmd_result(result, "run")
    assert wire.structuredContent["command"]["cmd_hash"] == result["cmd_hash"]


@pytest.mark.asyncio
async def test_request_cancellation_still_propagates_after_commit(tmp_path, monkeypatch):
    backend = await _backend(tmp_path, monkeypatch)
    backend.terminal.submit.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await backend.run("printf uncertain", **IDENTITY, queue_id=1, task_scope="none")
    assert _command_count(backend.repo) == 1


@pytest.mark.asyncio
async def test_concurrent_identical_runs_keep_distinct_durable_receipts(tmp_path, monkeypatch):
    backend = await _backend(tmp_path, monkeypatch)
    results = await asyncio.gather(*(
        backend.run("printf same", **IDENTITY, queue_id=1, task_scope="none")
        for _ in range(8)
    ))
    assert all(result["ok"] for result in results)
    assert len({result["cmd_hash"] for result in results}) == 8
    with sqlite3.connect(backend.repo.path) as connection:
        sequences = connection.execute(
            "SELECT queue_sequence FROM commands ORDER BY queue_sequence"
        ).fetchall()
        assert sequences == [(number,) for number in range(1, 9)]
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.asyncio
async def test_concurrent_cancel_retries_keep_one_durable_intent(tmp_path, monkeypatch):
    backend = await _backend(tmp_path, monkeypatch)
    command = await _attributed_command(backend)
    results = await asyncio.gather(*(
        backend.cancel(command.cmd_hash, **IDENTITY) for _ in range(8)
    ))
    assert all(result["ok"] for result in results)
    with sqlite3.connect(backend.repo.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM command_cancel_intents").fetchone() == (1,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.asyncio
async def test_reconciler_recovers_cancel_after_immediate_wakeup_failure(tmp_path, monkeypatch):
    from terminal_mcp.terminal.linux import LinuxTerminalAdapter

    backend = await _backend(tmp_path, monkeypatch)
    command = await _attributed_command(backend)
    scheduler = LinuxTerminalAdapter(
        backend.repo, "/bin/bash", tmp_path, 0.02, queue_reconcile_sec=0.01,
    )
    backend.terminal = scheduler
    with monkeypatch.context() as patch:
        patch.setattr(scheduler, "enqueue_cancel", Mock(side_effect=RuntimeError("wake-up")))
        result = await backend.cancel(command.cmd_hash, **IDENTITY)
    assert result["ok"] is True
    cancelled = asyncio.Event()

    async def finish_cancel(pending):
        pending.status = "cancelled"
        await backend.repo.update(pending)
        cancelled.set()

    monkeypatch.setattr(scheduler, "cancel", finish_cancel)
    monkeypatch.setattr(scheduler, "_recover_remote_running", AsyncMock())
    monkeypatch.setattr(scheduler, "_reconcile_processless_running", AsyncMock())
    worker = asyncio.create_task(scheduler._reconciler())
    try:
        await asyncio.wait_for(cancelled.wait(), timeout=2)
        assert (await backend.repo.get(command.cmd_hash)).status == "cancelled"
        assert await backend.repo.active_cancel_intents() == []
    finally:
        scheduler.stopping = True
        worker.cancel()
        await asyncio.gather(worker, *scheduler.cancel_tasks.values(), return_exceptions=True)
