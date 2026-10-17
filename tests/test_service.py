import asyncio

import pytest

from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


async def create_runtime(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000)
    return repo, terminal, service


async def wait_finished(service, cmd_hash, attempts=500):
    result = None
    for _ in range(attempts):
        result = await service.read(cmd_hash, 1000, 0)
        if result["status"] in {"completed", "failed", "cancelled"}:
            return result
        await asyncio.sleep(0.01)
    return result


def scoped_text(line):
    return line.split(" ", 1)[1]


def global_text(line):
    return line.split(" ", 4)[4]


@pytest.mark.asyncio
async def test_initialize_can_preserve_active_commands_for_reconnectable_execution(tmp_path):
    database = tmp_path / "reconnectable.sqlite3"
    repo = SqliteRepository(database)
    await repo.initialize()
    queued = await repo.create("printf queued", status="queued", queue_id=1)
    running = await repo.create("sleep 30", status="running", queue_id=1)

    restarted = SqliteRepository(database)
    await restarted.initialize(preserve_active_commands=True)

    assert (await restarted.get(queued.cmd_hash)).status == "queued"
    recovered = await restarted.get(running.cmd_hash)
    assert recovered.status == "running"
    assert recovered.error is None
    assert recovered.finished_at is None


@pytest.mark.asyncio
async def test_stdout_and_stderr_preserve_shell_order_and_exit_error_stays_null(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    command = (
        "printf 'stdout-one\\nstdout-two\\n'; "
        "printf 'stderr-one\\n' >&2; printf 'stdout-three\\n'; exit 4"
    )
    submitted = await service.run(command, task_scope="none")
    result = await wait_finished(service, submitted["cmd_hash"])
    assert result["status"] == "failed"
    assert result["exit_code"] == 4
    assert result["ok"] is True
    assert result["error"] is None
    assert [scoped_text(line) for line in result["lines"]] == [
        "stdout-one",
        "stdout-two",
        "stderr-one",
        "stdout-three",
    ]
    await terminal.stop()


@pytest.mark.asyncio
async def test_health_runs_optional_configured_command(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    service.health_command = "printf 'health-output\\n'"
    configured = await service.health("oauth")
    custom = configured["custom_command"]
    assert custom["command"] == service.health_command
    assert custom["status"] == "completed"
    assert custom["exit_code"] == 0
    assert custom["lines"][0].endswith("health-output")
    await terminal.stop()


@pytest.mark.asyncio
async def test_health_fails_storage_when_fleet_control_is_unhealthy(tmp_path):
    _repo, _terminal, service = await create_runtime(tmp_path)

    class BrokenFleetControl:
        async def healthy(self):
            return False

    service.fleet_control = BrokenFleetControl()
    result = await service.health("none")
    assert result["ok"] is False
    assert result["storage"] == "error"
