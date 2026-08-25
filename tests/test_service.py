import asyncio
import sqlite3
import stat

import pytest

import terminal_mcp.core.service as service_module
from terminal_mcp.auth.storage import OAuthStore
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
    return line.split("] ", 1)[1]


def global_text(line):
    return line.rsplit("] ", 1)[1]


@pytest.mark.asyncio
async def test_database_and_parent_are_root_only(tmp_path):
    database = tmp_path / "data" / "db.sqlite3"
    repo = SqliteRepository(database)
    await repo.initialize()
    assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600

    database.parent.chmod(0o755)
    database.chmod(0o644)
    oauth = OAuthStore(database)
    await oauth.initialize()
    assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_run_timeout_rolls_back_persisted_command(tmp_path, monkeypatch):
    repo, terminal, service = await create_runtime(tmp_path)
    monkeypatch.setattr(service_module, "OPERATION_TIMEOUT_SECONDS", 0.02)

    async def stalled_submit(command):
        await asyncio.sleep(1)

    monkeypatch.setattr(terminal, "submit", stalled_submit)
    result = await service.run("printf never-runs")
    assert result["ok"] is False
    assert result["cmd_hash"] is None
    assert result["error"].startswith("run.enqueue:")
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0
    await terminal.stop()


@pytest.mark.asyncio
async def test_scoped_read_defaults_to_last_500_and_supports_offsets(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    submitted = await service.run("seq 1 520")
    completed = await wait_finished(service, submitted["cmd_hash"])
    assert completed["status"] == "completed"

    latest = await service.read(submitted["cmd_hash"])
    assert latest["ok"] is True
    assert latest["overall_lines_count"] == 520
    assert latest["displayed_lines_count"] == 500
    assert latest["next_offset"] == 520
    assert scoped_text(latest["lines"][0]) == "21"
    assert scoped_text(latest["lines"][-1]) == "520"

    last_ten = await service.read(submitted["cmd_hash"], 10)
    assert [scoped_text(line) for line in last_ten["lines"]] == [str(i) for i in range(511, 521)]

    positive = await service.read(submitted["cmd_hash"], 3, 100)
    assert [scoped_text(line) for line in positive["lines"]] == ["101", "102", "103"]
    assert positive["next_offset"] == 103

    negative = await service.read(submitted["cmd_hash"], 3, -5)
    assert [scoped_text(line) for line in negative["lines"]] == ["516", "517", "518"]
    assert negative["next_offset"] == 518

    before_start = await service.read(submitted["cmd_hash"], 3, -5000)
    assert [scoped_text(line) for line in before_start["lines"]] == ["1", "2", "3"]

    after_end = await service.read(submitted["cmd_hash"], 3, 5000)
    assert after_end["lines"] == []
    assert after_end["next_offset"] == 520
    assert after_end["ok"] is True
    await terminal.stop()


@pytest.mark.asyncio
async def test_global_read_defaults_to_latest_500_without_count(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    submitted = await service.run("seq 1 600")
    await wait_finished(service, submitted["cmd_hash"])

    latest = await service.read()
    assert latest["overall_lines_count"] is None
    assert latest["displayed_lines_count"] == 500
    assert latest["next_offset"] > 0
    assert global_text(latest["lines"][0]) == "101"
    assert global_text(latest["lines"][-1]) == "600"

    negative = await service.read(None, 5, -10)
    assert [global_text(line) for line in negative["lines"]] == [
        "591",
        "592",
        "593",
        "594",
        "595",
    ]
    assert negative["next_offset"] > 0

    before_start = await service.read(None, 3, -5000)
    assert [global_text(line) for line in before_start["lines"]] == ["1", "2", "3"]

    positive = await service.read(None, 3, 2)
    assert [global_text(line) for line in positive["lines"]] == ["3", "4", "5"]
    assert positive["next_offset"] == 5

    cursor = latest["next_offset"]
    appended = await service.run("printf 'new-global-line\n'")
    await wait_finished(service, appended["cmd_hash"])
    incremental = await service.read(None, 10, cursor)
    assert [global_text(line) for line in incremental["lines"]] == ["new-global-line"]
    assert incremental["next_offset"] > cursor
    await terminal.stop()


@pytest.mark.asyncio
async def test_read_not_found_is_not_plugin_error(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    result = await service.read("deadbeef")
    assert result["ok"] is True
    assert result["status"] == "not_found"
    assert result["overall_lines_count"] == 0
    assert result["displayed_lines_count"] == 0
    assert result["error"] is None
    await terminal.stop()


@pytest.mark.asyncio
async def test_read_plugin_error_reports_stage():
    class BrokenRepo:
        async def get(self, cmd_hash):
            raise RuntimeError("database unavailable")

    service = TerminalService(BrokenRepo(), object(), 5000)
    result = await service.read("deadbeef")
    assert result["ok"] is False
    assert result["status"] is None
    assert result["error"] == "read.load_command: database unavailable"


@pytest.mark.asyncio
async def test_stdout_and_stderr_preserve_shell_order_and_exit_error_stays_null(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    command = (
        "printf 'stdout-one\\nstdout-two\\n'; "
        "printf 'stderr-one\\n' >&2; printf 'stdout-three\\n'; exit 4"
    )
    submitted = await service.run(command)
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
    plain = await service.health("oauth")
    assert "custom_command" not in plain

    service.health_command = "printf 'health-output\\n'"
    long_run = await service.run("sleep 30")
    await asyncio.sleep(0.05)
    configured = await asyncio.wait_for(service.health("oauth"), 1)
    await service.cancel(long_run["cmd_hash"])
    custom = configured["custom_command"]
    assert custom["command"] == service.health_command
    assert custom["status"] == "completed"
    assert custom["exit_code"] == 0
    assert custom["lines"][0].endswith("health-output")
    await terminal.stop()
