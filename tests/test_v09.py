import asyncio
import sqlite3
from datetime import timedelta

import pytest

from terminal_mcp.core.agent_policy import DEFAULT_SESSION_ALERT_MESSAGE, AgentPolicy
from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


async def runtime(tmp_path, **repo_kwargs):
    repo = SqliteRepository(tmp_path / "db.sqlite3", tmp_path / "output.sqlite3", **repo_kwargs)
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1, queue_reconcile_sec=0.01)
    service = TerminalService(repo, terminal, 5000)
    return repo, terminal, service


async def wait_done(service, cmd_hash, attempts=300):
    for _ in range(attempts):
        result = await service.read(cmd_hash, 1000, 0)
        if result["status"] in {"completed", "failed", "cancelled"}:
            return result
        await asyncio.sleep(0.01)
    return await service.read(cmd_hash, 1000, 0)


@pytest.mark.asyncio
async def test_output_line_command_caps_and_batched_writes(tmp_path, monkeypatch):
    repo, terminal, service = await runtime(
        tmp_path,
        output_line_max_bytes=128,
        output_command_max_bytes=256,
        output_target_bytes=1024,
        output_max_bytes=2048,
        output_max_rows=1000,
    )
    calls = 0
    real_append = repo.append_lines

    async def counted(cmd_hash, texts):
        nonlocal calls
        calls += 1
        return await real_append(cmd_hash, texts)

    monkeypatch.setattr(repo, "append_lines", counted)
    try:
        first = await service.run("python3 -c \"print('x'*300); print('tail')\"")
        first_read = await wait_done(service, first["cmd_hash"])
        assert first_read["output_truncated"] is True
        assert first_read["output_bytes"] <= 256
        assert first_read["overall_lines_count"] == 2
        assert first_read["lines"][-1].endswith("tail")
        assert "truncated" in first_read["lines"][0]

        calls = 0
        second = await service.run("seq 1 200")
        second_read = await wait_done(service, second["cmd_hash"])
        assert second_read["status"] == "completed"
        assert calls < 20

        third = await service.run("for i in $(seq 1 30); do printf '01234567890123456789\\n'; done")
        third_read = await wait_done(service, third["cmd_hash"])
        assert third_read["output_truncated"] is True
        assert third_read["output_bytes"] <= 256
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_retention_prunes_whole_old_commands_to_target(tmp_path):
    repo, terminal, service = await runtime(
        tmp_path,
        output_line_max_bytes=256,
        output_command_max_bytes=256,
        output_target_bytes=300,
        output_max_bytes=400,
        output_max_rows=1000,
    )
    try:
        hashes = []
        for marker in ("a", "b", "c"):
            run = await service.run(f"python3 -c \"print('{marker}'*180)\"")
            done = await wait_done(service, run["cmd_hash"])
            assert done["status"] == "completed"
            hashes.append(run["cmd_hash"])
        stats = await repo.output_cache_stats()
        assert stats["used_bytes"] <= 300
        assert stats["retained_commands"] == 1
        assert (await service.read(hashes[0]))["output_retained"] is False
        assert (await service.read(hashes[1]))["output_retained"] is False
        newest = await service.read(hashes[2])
        assert newest["output_retained"] is True
        assert newest["overall_lines_count"] == 1
        assert stats["last_prune_at"]
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_v4_lines_migrate_and_duplicate_index_is_removed(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    output = tmp_path / "output.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript(
            """
            CREATE TABLE commands(
                hash TEXT PRIMARY KEY,cmd TEXT,status TEXT,pid INTEGER,exit_code INTEGER,error TEXT,
                started_at TEXT,finished_at TEXT,queue_id INTEGER,queue_sequence INTEGER,
                enqueued_at TEXT,claimed_at TEXT
            );
            CREATE TABLE lines(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                hash TEXT, appeared_at TEXT, text TEXT
            );
            CREATE INDEX ix_lines_hash_seq ON lines(hash,seq);
            CREATE INDEX idx_lines_hash_seq ON lines(hash,seq);
            INSERT INTO commands(hash,cmd,status,finished_at)
                VALUES('deadbeef','printf old','completed','2026-09-22T00:00:00.000Z');
            INSERT INTO lines(hash,appeared_at,text) VALUES('deadbeef','12:00:00','legacy-output');
            PRAGMA user_version=4;
            """
        )
        db.commit()

    repo = SqliteRepository(database, output, output_target_bytes=1024, output_max_bytes=2048)
    await repo.initialize()
    with sqlite3.connect(database) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "lines" not in tables
        assert "ix_lines_hash_seq" not in indexes
        assert "idx_lines_hash_seq" not in indexes
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
    with sqlite3.connect(output) as db:
        indexes = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "ix_lines_hash_seq" in indexes
        assert "idx_lines_hash_seq" not in indexes
    lines = await repo.read_command_lines("deadbeef", 10, 0)
    assert [line.text for line in lines] == ["legacy-output"]


@pytest.mark.asyncio
async def test_session_warning_alert_repeat_and_hard_expiry(tmp_path):
    policy = AgentPolicy(
        max_session_seconds=100,
        session_warning_after_seconds=60,
        session_alert_enabled=True,
        session_alert_after_seconds=80,
        session_alert_repeat_seconds=10,
    )
    repo = SqliteRepository(tmp_path / "db.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000, agent_policy=policy)
    try:
        started = await service.agent_start(
            task_summary="Session policy",
            intent="Validate timers",
            details=["Validate"],
            work_scope=["test"],
        )
        agent_id = started["self"]["agent_id"]
        with sqlite3.connect(repo.path) as db:
            db.execute(
                "UPDATE agent_sessions SET registered_at=? WHERE agent_id=?",
                (utc_text(utc_now() - timedelta(seconds=61)), agent_id),
            )
            db.commit()
        warned = await service.health("none", agent_id=agent_id)
        assert warned["session_warning"]
        assert warned["alert_pending"] is False

        with sqlite3.connect(repo.path) as db:
            db.execute(
                "UPDATE agent_sessions SET registered_at=? WHERE agent_id=?",
                (utc_text(utc_now() - timedelta(seconds=81)), agent_id),
            )
            db.commit()
        blocked = await service.run("printf blocked", agent_id=agent_id)
        assert blocked["ok"] is False
        assert blocked["alert_pending"] is True
        assert DEFAULT_SESSION_ALERT_MESSAGE in blocked["alert_messages"][0]
        journal = await AgentStore(repo.path).message_journal(agent_id)
        first = next(item for item in journal if item["sender_agent_id"] == "system-session")
        first_hash = first["message_hash"]
        await service.message(
            agent_id, message_hash=first_hash, text="Завершаю сессию и возвращаюсь"
        )
        allowed = await service.run("printf allowed", agent_id=agent_id, queue_id=1)
        assert allowed["ok"] is True

        with sqlite3.connect(repo.path) as db:
            old = utc_text(utc_now() - timedelta(seconds=11))
            db.execute(
                "UPDATE coordination_messages SET created_at=? WHERE message_hash=?",
                (old, first_hash),
            )
            db.execute(
                "UPDATE coordination_message_recipients SET replied_at=? "
                "WHERE message_hash=? AND recipient_agent_id=?",
                (old, first_hash, agent_id),
            )
            db.commit()
        repeated = await service.health("none", agent_id=agent_id)
        assert repeated["alert_pending"] is True
        journal = await AgentStore(repo.path).message_journal(agent_id)
        system_alerts = [item for item in journal if item["sender_agent_id"] == "system-session"]
        assert len(system_alerts) == 2
        assert system_alerts[0]["message_hash"] != first_hash

        with sqlite3.connect(repo.path) as db:
            db.execute(
                "UPDATE agent_sessions SET registered_at=? WHERE agent_id=?",
                (utc_text(utc_now() - timedelta(seconds=101)), agent_id),
            )
            db.commit()
        expired = await service.health("none", agent_id=agent_id)
        assert expired["session_status"] == "forced"
        assert expired["session_end_reason"] == "max_session_duration"
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_session_alert_can_be_disabled(tmp_path):
    policy = AgentPolicy(
        max_session_seconds=100,
        session_warning_after_seconds=60,
        session_alert_enabled=False,
    )
    repo = SqliteRepository(tmp_path / "db.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000, agent_policy=policy)
    try:
        started = await service.agent_start(
            task_summary="No alert",
            intent="Validate disabled timer",
            details=["Validate"],
            work_scope=["test"],
        )
        agent_id = started["self"]["agent_id"]
        with sqlite3.connect(repo.path) as db:
            db.execute(
                "UPDATE agent_sessions SET registered_at=? WHERE agent_id=?",
                (utc_text(utc_now() - timedelta(seconds=90)), agent_id),
            )
            db.commit()
        health = await service.health("none", agent_id=agent_id)
        assert health["session_warning"]
        assert health["alert_pending"] is False
    finally:
        await terminal.stop()
