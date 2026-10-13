"""Service/store fixture boundaries for the agent acceptance checks."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.sqlite import SqliteRepository


async def runtime(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    terminal = SimpleNamespace(stop=AsyncMock())
    service = TerminalService(repo, terminal, 5000)
    return repo, terminal, service


async def native_runtime(tmp_path):
    from terminal_mcp.terminal.linux import LinuxTerminalAdapter

    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000)
    return repo, terminal, service


async def register(service, summary, intent, work_scope=None, details=None):
    plan = details or [intent]
    proposed = await service.agent_start(
        task_summary=summary,
        intent=intent,
        details=plan,
        work_scope=work_scope,
    )
    assert proposed["admission_required"] is True
    assert proposed["ok"] is False
    return await service.agent_start(
        agent_id=proposed["proposed_agent_id"],
        task_summary=summary,
        intent=intent,
        details=plan,
        work_scope=work_scope,
    )


async def wait_finished(service, agent_id, cmd_hash, attempts=200):
    result = None
    for _ in range(attempts):
        result = await service.read(cmd_hash, 100, 0, agent_id=agent_id)
        if result["status"] in {"completed", "failed", "cancelled"}:
            return result
        await asyncio.sleep(0.01)
    return result
