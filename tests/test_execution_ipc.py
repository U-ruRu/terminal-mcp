"""Current Unix IPC fencing, bounded execution and durable output replay."""

from __future__ import annotations

import asyncio
import os
import pwd

import pytest

from terminal_mcp.core.execution import ExecutionPortError, ExecutionRequest
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.executor_service import ExecutorService
from terminal_mcp.terminal.ipc import UnixExecutionAdapter


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


async def test_wrong_peer_uid_denied_without_starting_any_process(tmp_path):
    service = ExecutorService(tmp_path / "deny.sock", allowed_uids=[os.getuid() + 10000])
    await service.start()
    try:
        with pytest.raises(ExecutionPortError, match="execution_peer_denied"):
            await adapter(service)
        assert not service.spools
    finally:
        await service.close()


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


async def test_capture_timeout_terminates_owned_process(executor):
    port = await adapter(executor)
    try:
        result = await port.capture("sleep 10", timeout_ms=20, timeout_error="deadline")

        assert result["error"] == "deadline"
        assert not executor.spools
    finally:
        await port.close()


async def test_replayed_output_commits_only_the_unseen_suffix(tmp_path):
    repo = SqliteRepository(tmp_path / "replay.sqlite3")
    await repo.initialize()
    await repo.append_replayed_lines("key", ["a", "b"], 0)

    await repo.append_replayed_lines("key", ["b", "c"], 1)

    assert [line.text for line in await repo.read_lines("key", 10, 0)] == ["a", "b", "c"]


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
