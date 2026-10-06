"""Architecture C: real Unix IPC, ownership, replay and application restart recovery."""

from __future__ import annotations

import asyncio
import os
import pwd
import stat
from dataclasses import asdict, replace

import pytest

from terminal_mcp.application.command_scheduler import CommandScheduler
from terminal_mcp.core.execution import ExecutionPort, ExecutionPortError, ExecutionRequest
from terminal_mcp.core.service import TerminalService
from terminal_mcp.mcp.output_contracts import cmd_result
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.executor_service import ExecutorService
from terminal_mcp.terminal.ipc import UnixExecutionAdapter, encode_frame, read_frame


async def eventually(predicate, budget=8):
    changed = asyncio.Event()
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(budget):
        while not await predicate():
            tick = loop.call_later(0.01, changed.set)
            try:
                await changed.wait()
            finally:
                tick.cancel()
                changed.clear()


@pytest.fixture
async def executor(tmp_path):
    server = ExecutorService(
        tmp_path / "e.sock", allowed_uids=[os.getuid()], user=pwd.getpwuid(os.getuid()).pw_name
    )
    await server.start()
    try:
        yield server
    finally:
        await server.close()


async def adapter(server):
    port = UnixExecutionAdapter(server.path, rpc_timeout=3)
    await port.start()
    return port


async def read_all(port, handle):
    data = bytearray()
    async with asyncio.timeout(5):
        while chunk := await port.read_output(handle, 1024):
            data.extend(chunk)
    return bytes(data)


async def test_real_unix_port_replay_fencing_and_bounded_output(executor, tmp_path):
    port = await adapter(executor)
    assert isinstance(port, ExecutionPort)
    assert stat.S_IMODE(executor.path.stat().st_mode) == 0o660
    handles = await asyncio.gather(*(port.spawn(ExecutionRequest("one")) for _ in range(4)))
    assert len(set(handles)) == 1
    handle = handles[0]
    marker = tmp_path / "marker"
    command = f"echo once >> '{marker}'; printf 'hello\\nworld\\n'"
    await asyncio.gather(*(port.write_stdin(handle, command) for _ in range(3)))
    with pytest.raises(ExecutionPortError, match="execution_input_conflict"):
        await port.write_stdin(handle, "echo wrong")
    with pytest.raises(ExecutionPortError, match="execution_not_owned"):
        await port.signal(replace(handle, token="not-the-owner"), "kill")
    with pytest.raises(ExecutionPortError, match="execution_read_bound_invalid"):
        await port.read_output(handle, True)
    assert await port.wait(handle, 3) == 0
    # Reading the same byte offset after an ambiguous/disconnected reply is not destructive.
    first = await port._rpc("read_output", handle=asdict(handle), offset=0, max_bytes=5)
    second = await port._rpc("read_output", handle=asdict(handle), offset=0, max_bytes=5)
    assert first["data"] == second["data"]
    assert await read_all(port, handle) == b"hello\nworld\n"
    assert marker.read_text() == "once\n"
    await port.release(handle)
    await port.release(handle)
    with pytest.raises(ExecutionPortError, match="execution_released"):
        await port.spawn(ExecutionRequest("one"))
    await port.close()


async def test_client_takeover_preserves_process_and_fences_old_handle(executor):
    old = await adapter(executor)
    handle = await old.spawn(ExecutionRequest("surviving"))
    await old.write_stdin(handle, "sleep 0.1; echo survived")
    new = await adapter(executor)
    with pytest.raises(ExecutionPortError, match="execution_client_fenced"):
        await old.signal(handle, "kill")
    recovered = await new.recover("surviving")
    assert recovered == handle and await new.input_written(handle)
    assert await new.wait(handle, 2) == 0
    assert await read_all(new, handle) == b"survived\n"
    assert await new.recover("missing") is None
    await new.release(handle)


async def test_wrong_peer_uid_denied_without_starting_any_process(tmp_path):
    service = ExecutorService(tmp_path / "deny.sock", allowed_uids=[os.getuid() + 10000])
    await service.start()
    try:
        with pytest.raises(ExecutionPortError, match="execution_peer_denied"):
            await adapter(service)
        assert not service.spools
    finally:
        await service.close()


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"version": True},
        {"extra": "field"},
        {"args": []},
    ],
)
async def test_strict_protocol_rejects_unknown_or_wrong_types(executor, change):
    request = {"version": 1, "client": "client", "id": "request", "op": "hello", "args": {}}
    request.update(change)
    reader, writer = await asyncio.open_unix_connection(str(executor.path))
    try:
        writer.write(encode_frame(request))
        await writer.drain()
        result = await read_frame(reader)
        assert result["ok"] is False
        assert result["error"] in ("execution_version_unsupported", "execution_argument_invalid")
        assert not executor.spools
    finally:
        writer.close()
        await writer.wait_closed()


async def test_socket_collision_never_unlinks_file_or_existing_server(executor, tmp_path):
    second = ExecutorService(executor.path, allowed_uids=[os.getuid()])
    with pytest.raises(ExecutionPortError, match="execution_socket_exists"):
        await second.start()
    port = await adapter(executor)
    assert (await port.health())["shell"] == "/bin/bash"
    regular = tmp_path / "important"
    regular.write_text("keep")
    with pytest.raises(ExecutionPortError, match="execution_socket_exists"):
        await ExecutorService(regular, allowed_uids=[os.getuid()]).start()
    assert regular.read_text() == "keep"


async def test_missing_executor_never_falls_back_to_local_execution(tmp_path):
    port = UnixExecutionAdapter(tmp_path / "missing.sock", rpc_timeout=0.1)
    with pytest.raises(ExecutionPortError, match="execution_unavailable"):
        await port.start()


async def test_spool_is_bounded_and_truncation_is_explicit(tmp_path):
    service = ExecutorService(
        tmp_path / "bounded.sock",
        allowed_uids=[os.getuid()],
        spool_max_bytes=128,
        user=pwd.getpwuid(os.getuid()).pw_name,
    )
    await service.start()
    try:
        port = await adapter(service)
        handle = await port.spawn(ExecutionRequest("bounded"))
        await port.write_stdin(handle, "printf '%01000d' 0")
        assert await port.wait(handle, 2) == 0
        assert len(await read_all(port, handle)) == 128
        assert await port.output_truncated(handle)
        await port.release(handle)
    finally:
        await service.close()


async def test_capture_is_bounded_and_timeout_kills_owned_process(executor):
    port = await adapter(executor)
    result = await port.capture("echo capture", timeout_ms=1000)
    assert result["ok"] and result["lines"][0].endswith("capture")
    result = await port.capture("sleep 10", timeout_ms=20, timeout_error="deadline")
    assert not result["ok"] and result["error"] == "deadline"
    assert not executor.spools


async def test_api_restart_replays_unacknowledged_output_exactly_once(executor, tmp_path):
    repo = SqliteRepository(tmp_path / "state.sqlite3")
    await repo.initialize()
    first = CommandScheduler(repo, await adapter(executor), 0.1, queue_reconcile_sec=0.05)
    service = TerminalService(repo, first, 5000)
    marker, gate = tmp_path / "once", tmp_path / "gate"
    command = (
        f"echo once >> '{marker}'; for i in $(seq 1 80); do echo line-$i; done; "
        f"while [ ! -f '{gate}' ]; do sleep 0.03; done; echo tail"
    )
    new = None
    try:
        result = await service.run(command, queue_id=1, task_scope="none")
        key = result["cmd_hash"]

        async def committed():
            return await repo.output.count_lines(key) >= 64

        await eventually(committed)
        tail = await service.run("echo queue-tail", queue_id=1, task_scope="none")
        await first.stop()
        assert (await repo.get(key)).status == "running"
        assert key in executor.spools
        assert (await repo.get(tail["cmd_hash"])).status == "queued"

        # A real API restart reinitializes the durable store before the scheduler
        # can ask the still-running executor to reattach. Preserve both the remote
        # running owner and the application-owned queued tail until that arbitration.
        repo = SqliteRepository(tmp_path / "state.sqlite3")
        await repo.initialize(preserve_active_commands=True)
        assert (await repo.get(key)).status == "running"
        assert (await repo.get(tail["cmd_hash"])).status == "queued"

        new = CommandScheduler(repo, await adapter(executor), 0.1, queue_reconcile_sec=0.05)
        await new.start()
        gate.touch()

        async def done():
            return (await repo.get(tail["cmd_hash"])).status == "completed"

        await eventually(done)
        assert (await repo.get(key)).status == "completed"
        lines = await repo.read_lines(key, 1000, 0)
        assert [line.text for line in lines] == [f"line-{i}" for i in range(1, 81)] + ["tail"]
        assert marker.read_text() == "once\n"
        assert not (await repo.output_status(key))["output_truncated"]
    finally:
        gate.touch()
        await first.stop()
        if new:
            await new.stop()


async def test_executor_loss_marks_failed_without_replaying_side_effect(tmp_path):
    path = tmp_path / "loss.sock"
    repo = SqliteRepository(tmp_path / "loss.sqlite3")
    await repo.initialize()
    server = ExecutorService(
        path, allowed_uids=[os.getuid()], user=pwd.getpwuid(os.getuid()).pw_name
    )
    await server.start()
    first = CommandScheduler(repo, await adapter(server), 0.1, queue_reconcile_sec=0.05)
    service = TerminalService(repo, first, 1000)
    marker = tmp_path / "side-effect"
    new = None
    try:
        result = await service.run(f"echo once >> '{marker}'; sleep 20", task_scope="none")
        key = result["cmd_hash"]

        async def written():
            return marker.exists()

        await eventually(written)
        await first.stop()
        await server.close()
        server = ExecutorService(
            path, allowed_uids=[os.getuid()], user=pwd.getpwuid(os.getuid()).pw_name
        )
        await server.start()
        new = CommandScheduler(repo, await adapter(server), 0.1, queue_reconcile_sec=0.05)
        await new.start()
        assert (await repo.get(key)).status == "failed"
        assert "executor_lost" in (await repo.get(key)).error
        assert (await repo.output_status(key))["output_truncated"]
        read = await service.read(key, 1000, 0)
        assert read["ok"] is True
        assert read["status"] == "failed"
        assert "executor_lost" in read["error"]
        public = cmd_result(read, "read").structuredContent
        assert public["ok"] is True
        assert public["command"]["status"] == "failed"
        assert marker.read_text() == "once\n"
        assert not server.spools
    finally:
        await first.stop()
        if new:
            await new.stop()
        await server.close()


async def test_output_replay_commit_is_atomic_and_rejects_gaps(tmp_path):
    repo = SqliteRepository(tmp_path / "replay.sqlite3")
    await repo.initialize()
    await asyncio.gather(*(repo.append_replayed_lines("key", ["a", "b"], 0) for _ in range(4)))
    await repo.append_replayed_lines("key", ["b", "c"], 1)
    assert [line.text for line in await repo.read_lines("key", 10, 0)] == ["a", "b", "c"]
    with pytest.raises(ValueError, match="output replay gap"):
        await repo.append_replayed_lines("key", ["gap"], 4)


async def test_old_client_cannot_steal_generation_back(executor):
    old = await adapter(executor)
    new = await adapter(executor)
    with pytest.raises(ExecutionPortError, match="execution_client_fenced"):
        await old.start()
    assert (await new.health())["shell"] == "/bin/bash"


async def test_stale_socket_after_hard_crash_is_reclaimed(tmp_path):
    import socket

    path = tmp_path / "stale.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    server = ExecutorService(path, allowed_uids=[os.getuid()])
    await server.start()
    try:
        port = await adapter(server)
        assert (await port.health())["shell"] == "/bin/bash"
    finally:
        await server.close()


async def test_executor_restart_converges_without_api_restart(tmp_path):
    path = tmp_path / "restart.sock"
    server = ExecutorService(
        path, allowed_uids=[os.getuid()], user=pwd.getpwuid(os.getuid()).pw_name
    )
    await server.start()
    repo = SqliteRepository(tmp_path / "restart.sqlite3")
    await repo.initialize()
    scheduler = CommandScheduler(repo, await adapter(server), 0.1, queue_reconcile_sec=0.05)
    service = TerminalService(repo, scheduler, 1000)
    marker = tmp_path / "once"
    try:
        result = await service.run(f"echo once >> '{marker}'; sleep 20", task_scope="none")

        async def running():
            return marker.exists()

        await eventually(running)
        await server.close()
        server = ExecutorService(
            path, allowed_uids=[os.getuid()], user=pwd.getpwuid(os.getuid()).pw_name
        )
        await server.start()

        async def failed():
            return (await repo.get(result["cmd_hash"])).status == "failed"

        await eventually(failed)
        assert marker.read_text() == "once\n"
        assert (await repo.output_status(result["cmd_hash"]))["output_truncated"]
        following = await service.run("echo after-restart", task_scope="none")

        async def completed():
            return (await repo.get(following["cmd_hash"])).status == "completed"

        await eventually(completed)
    finally:
        await scheduler.stop()
        await server.close()


async def test_capture_cancellation_reaches_executor_and_joins_process(executor):
    port = await adapter(executor)
    capture = asyncio.create_task(port.capture("sleep 60", timeout_ms=10000))

    async def started():
        return bool(executor.spools)

    await eventually(started)
    capture.cancel()
    with pytest.raises(asyncio.CancelledError):
        await capture
    assert not executor.spools
    assert not executor.native._owned
