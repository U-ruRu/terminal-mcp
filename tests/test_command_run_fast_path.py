import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import terminal_mcp.core.persistent_backend as persistent_backend_module
from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.commands import CommandApplication
from terminal_mcp.application.requests import CmdReadRequest, CmdRecoveryRequest, CmdRunRequest
from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.mcp.output_contracts import cmd_result
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


class _Lifecycle:
    @asynccontextmanager
    async def operation_guard(self, logical_agent_id):
        yield


class _FakeRepo:
    def __init__(self, mode):
        self.mode = mode
        self.commands = {}

    async def create(self, cmd, *, cmd_hash, status, queue_id, **kwargs):
        command = SimpleNamespace(
            cmd=cmd,
            cmd_hash=cmd_hash,
            status=status,
            queue_id=queue_id,
            exit_code=None,
            claimed_at=None,
            started_at=None,
            finished_at=None,
        )
        self.commands[cmd_hash] = command
        return command

    async def get(self, cmd_hash):
        return self.commands.get(cmd_hash)

    async def queue_position(self, cmd_hash):
        command = self.commands.get(cmd_hash)
        return 1 if command is not None and command.status == "queued" else None

    async def queue_snapshot(self, queue_count):
        result = [
            {"queue_id": queue_id, "running": None, "queued": 0}
            for queue_id in range(1, queue_count + 1)
        ]
        if self.mode == "backlogged":
            command = next(iter(self.commands.values()))
            result[command.queue_id - 1] = {
                "queue_id": command.queue_id,
                "running": "older-command",
                "queued": 1,
            }
        return result


class _FakeTerminal:
    queue_workers = 4

    def __init__(self, repo, mode, default_queue=1):
        self.repo = repo
        self.mode = mode
        self.default_queue = default_queue
        self.finish_tasks = []

    async def least_loaded_queue(self):
        return self.default_queue

    async def submit(self, command):
        if self.mode == "backlogged":
            return
        command.status = "running"
        command.claimed_at = "2026-10-06T00:00:00Z"
        command.started_at = command.claimed_at
        if self.mode in {"completed", "failed"}:
            self.finish_tasks.append(asyncio.create_task(self._finish(command)))

    async def _finish(self, command):
        await asyncio.sleep(0.02)
        command.status = self.mode
        command.exit_code = 0 if self.mode == "completed" else 7
        command.finished_at = "2026-10-06T00:00:01Z"

    async def close(self):
        if self.finish_tasks:
            await asyncio.gather(*self.finish_tasks)


class _FastPathBackend(PersistentBackend):
    async def _execution_authority(self, *args, **kwargs):
        session = SimpleNamespace(
            hard_expires_at="2099-01-01T00:00:00Z", authority_node_id="node-a"
        )
        return session, None

    async def _task_refs(self, logical_agent_id):
        return []


def _fake_backend(mode, *, default_queue=1):
    repo = _FakeRepo(mode)
    terminal = _FakeTerminal(repo, mode, default_queue=default_queue)
    service = SimpleNamespace(
        repo=repo,
        terminal=terminal,
        task_coordinator=None,
        task_store=None,
        agent_policy=SimpleNamespace(command_preview_chars=160),
    )
    return terminal, _FastPathBackend(service, _Lifecycle())


def _run_kwargs(queue_id=1):
    return {
        "logical_agent_id": "logical-1",
        "work_session_id": "ws-1",
        "session_epoch": 1,
        "queue_id": queue_id,
        "task_scope": "none",
    }


@pytest.mark.asyncio
async def test_default_queue_prefers_idle_then_deterministic_tie(tmp_path):
    repo = SqliteRepository(tmp_path / "state.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.02)
    await repo.create("one", status="running", queue_id=1)
    await repo.create("two", queue_id=2)
    assert await terminal.least_loaded_queue() == 3
    await repo.create("three", queue_id=3)
    await repo.create("four", queue_id=4)
    assert await terminal.least_loaded_queue() == 1


@pytest.mark.asyncio
async def test_fast_run_returns_completed_or_failed_without_followup_read(monkeypatch):
    monkeypatch.setattr(persistent_backend_module, "COMMAND_RUN_INLINE_BUDGET_SECONDS", 1.0)

    terminal, backend = _fake_backend("completed")
    completed = await backend.run("printf 'fast\\n'", **_run_kwargs(queue_id=4))
    assert completed["status"] == "completed"
    assert completed["exit_code"] == 0
    assert completed["execution_started"] is True
    assert completed["queue_id"] == 4
    await terminal.close()

    terminal, backend = _fake_backend("failed")
    failed = await backend.run("printf 'bad\\n'; exit 7", **_run_kwargs(queue_id=4))
    assert failed["status"] == "failed"
    assert failed["exit_code"] == 7
    assert failed["execution_started"] is True
    await terminal.close()


@pytest.mark.asyncio
async def test_backlogged_run_returns_queued_promptly(monkeypatch):
    monkeypatch.setattr(persistent_backend_module, "COMMAND_RUN_INLINE_BUDGET_SECONDS", 1.0)
    _terminal, backend = _fake_backend("backlogged")
    started = time.monotonic()
    queued = await backend.run("printf 'later\\n'", **_run_kwargs(queue_id=1))
    elapsed = time.monotonic() - started
    assert queued["status"] == "queued"
    assert queued["queue_position"] == 1
    assert queued["execution_started"] is False
    assert elapsed < 0.2


@pytest.mark.asyncio
async def test_slow_started_run_returns_running_at_inline_deadline(monkeypatch):
    monkeypatch.setattr(persistent_backend_module, "COMMAND_RUN_INLINE_BUDGET_SECONDS", 0.15)
    _terminal, backend = _fake_backend("running")
    started = time.monotonic()
    result = await backend.run("sleep 2", **_run_kwargs(queue_id=2))
    elapsed = time.monotonic() - started
    assert result["status"] == "running"
    assert result["execution_started"] is True
    assert 0.1 <= elapsed < 0.5


@pytest.mark.asyncio
async def test_implicit_queue_selection_remains_authoritative():
    terminal, backend = _fake_backend("completed", default_queue=3)
    result = await backend.run("printf 'fast\\n'", **_run_kwargs(queue_id=None))
    assert result["queue_id"] == 3
    await terminal.close()


class _Gate:
    async def identity(self, actor, code, operation=None):
        return {
            "logical_agent_id": "logical-1",
            "work_session_id": "ws-1",
            "session_epoch": 1,
        }, None

    async def command_state(self, actor, identity, action):
        return {}, None


class _CompletedBackend:
    async def run(self, *args, **kwargs):
        return {
            "ok": True,
            "cmd_hash": "deadbeef",
            "status": "completed",
            "queue_id": 1,
            "queue_position": None,
            "exit_code": 0,
            "execution_started": True,
            "task_scope": "none",
            "task_targets": [],
            "task_scope_options": ["none"],
        }


class _CompletedService:
    def __init__(self):
        self.persistent = _CompletedBackend()

    async def read(self, **kwargs):
        assert kwargs["offset"] == 0
        return {
            "ok": True,
            "cmd_hash": "deadbeef",
            "status": "completed",
            "queue_id": 1,
            "queue_position": None,
            "exit_code": 0,
            "execution_started": True,
            "lines": ["first", "second"],
            "next_offset": 2,
            "overall_lines_count": 2,
            "displayed_lines_count": 2,
            "output_truncated": False,
            "output_retained": True,
            "output_pruned_at": None,
            "output_bytes": 11,
            "error": None,
        }


@pytest.mark.asyncio
async def test_command_application_adds_canonical_first_page_to_fast_run():
    app = CommandApplication(_CompletedService(), _Gate())
    result = await app.cmd(
        ActorContext(),
        CmdRunRequest(action="run", code="1234", command="printf fast", task_scope="none"),
    )
    assert result["status"] == "completed"
    assert result["lines"] == ["first", "second"]
    assert result["displayed_lines_count"] == 2
    assert result["has_more"] is False
    assert result["next_cursor"] is None

    wire = cmd_result(result, "run")
    assert wire.structuredContent["command"]["status"] == "completed"
    assert wire.structuredContent["lines"] == ["first", "second"]
    assert wire.structuredContent["displayed_lines_count"] == 2


@pytest.mark.asyncio
async def test_provider_code_free_read_uses_managed_gate_not_anonymous_fast_path():
    class ManagedReadGate(_Gate):
        def __init__(self):
            self.provider_identity_called = False

        async def provider_identity(self, actor, operation):
            self.provider_identity_called = True
            assert actor.provider == "openai"
            assert actor.provider_metadata["openai/session"] == "conversation-1"
            return SimpleNamespace(
                identity={
                    "logical_agent_id": "logical-1",
                    "public_name": "Alpha-1",
                    "authority_node_id": "node-a",
                },
                failure=None,
            )

    gate = ManagedReadGate()
    app = CommandApplication(_CompletedService(), gate)
    result = await app.cmd(
        ActorContext(
            provider="openai",
            provider_metadata={
                "openai/subject": "user-1",
                "openai/session": "conversation-1",
            },
        ),
        CmdReadRequest(action="read", cmd_hash="deadbeef"),
    )
    assert gate.provider_identity_called is True
    assert result["logical_agent_id"] == "logical-1"
    assert result["public_name"] == "Alpha-1"
    assert result["work_session_id"] is None


@pytest.mark.asyncio
async def test_same_agent_second_run_enqueues_during_first_inline_poll(monkeypatch):
    monkeypatch.setattr(persistent_backend_module, "COMMAND_RUN_INLINE_BUDGET_SECONDS", 0.4)
    monkeypatch.setattr(persistent_backend_module, "COMMAND_RUN_INLINE_POLL_SECONDS", 0.01)

    class SerialLifecycle:
        def __init__(self):
            self.lock = asyncio.Lock()

        @asynccontextmanager
        async def operation_guard(self, logical_agent_id):
            async with self.lock:
                yield

    class ConcurrentRepo(_FakeRepo):
        def __init__(self):
            super().__init__("running")
            self.first_created = asyncio.Event()
            self.second_created = asyncio.Event()

        async def create(self, *args, **kwargs):
            command = await super().create(*args, **kwargs)
            if len(self.commands) == 1:
                self.first_created.set()
            elif len(self.commands) == 2:
                self.second_created.set()
            return command

    class ConcurrentTerminal(_FakeTerminal):
        def __init__(self, repo):
            super().__init__(repo, "running")
            self.selection_count = 0

        async def least_loaded_queue(self):
            self.selection_count += 1
            return self.selection_count

    repo = ConcurrentRepo()
    terminal = ConcurrentTerminal(repo)
    service = SimpleNamespace(
        repo=repo,
        terminal=terminal,
        task_coordinator=None,
        task_store=None,
        agent_policy=SimpleNamespace(command_preview_chars=160),
    )
    backend = _FastPathBackend(service, SerialLifecycle())

    first = asyncio.create_task(backend.run("sleep 2", **_run_kwargs(queue_id=None)))
    await asyncio.wait_for(repo.first_created.wait(), timeout=0.1)
    second = asyncio.create_task(backend.run("sleep 2", **_run_kwargs(queue_id=None)))

    await asyncio.wait_for(repo.second_created.wait(), timeout=0.15)
    assert sorted(command.queue_id for command in repo.commands.values()) == [1, 2]

    first_result, second_result = await asyncio.gather(first, second)
    assert first_result["status"] == "running"
    assert second_result["status"] == "running"


@pytest.mark.asyncio
async def test_command_recovery_uses_work_session_id_as_public_session_ref():
    class RecoveryBackend:
        async def recovery(self, command, **kwargs):
            assert command == "printf recovery"
            assert kwargs["work_session_id"] == "ws-1"
            return {
                "ok": True,
                "cmd_hash": "cafebabe",
                "status": "completed",
                "exit_code": 0,
                "lines": ["recovered"],
                "overall_lines_count": 1,
                "displayed_lines_count": 1,
                "output_truncated": False,
                "output_retained": True,
            }

    class RecoveryService:
        persistent = RecoveryBackend()

    class RecoveryGate(_Gate):
        async def identity(self, actor, code, operation=None):
            return {
                "logical_agent_id": "logical-1",
                "work_session_id": "ws-1",
                "session_epoch": 1,
                "public_name": "Alpha-1",
            }, None

    app = CommandApplication(RecoveryService(), RecoveryGate())
    result = await app.cmd(
        ActorContext(endpoint_role="executor"),
        CmdRecoveryRequest(action="recovery", command="printf recovery"),
    )

    assert result["ok"] is True
    assert result["session_ref"] == "ws-1"
    assert result["public_name"] == "Alpha-1"
