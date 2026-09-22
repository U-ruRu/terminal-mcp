import asyncio
import sqlite3
from datetime import timedelta

import pytest

from terminal_mcp.core.agent_policy import AgentPolicy
from terminal_mcp.core.orchestration import public_agent_name, utc_now, utc_text
from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


async def runtime(tmp_path, *, policy=None, workers=4, reconcile=0.05):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(
        repo, "/bin/bash", tmp_path, 0.1, queue_workers=workers, queue_reconcile_sec=reconcile
    )
    service = TerminalService(repo, terminal, 5000, agent_policy=policy)
    return repo, terminal, service


async def register(service, summary="Task", intent="Work"):
    return await service.agent_start(
        task_summary=summary,
        intent=intent,
        details=["Inspect", "Implement", "Validate"],
        work_scope=["repo:test"],
    )


async def wait_status(service, cmd_hash, statuses, attempts=200):
    expected = {statuses} if isinstance(statuses, str) else set(statuses)
    result = None
    for _ in range(attempts):
        result = await service.read(cmd_hash, 100, 0)
        if result["status"] in expected:
            return result
        await asyncio.sleep(0.01)
    return result


@pytest.mark.asyncio
async def test_seen_message_repeats_until_explicit_read_ack(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    try:
        sender = (await register(service, "Sender", "Coordinate"))["self"]["agent_id"]
        receiver = (await register(service, "Receiver", "Implement"))["self"]["agent_id"]
        sent = await service.message(
            sender, target=public_agent_name(receiver), text="Read this carefully"
        )
        message_hash = sent["message_hash"]

        first = await service.read(agent_id=receiver)
        assert first["ok"] is True
        assert any(message_hash in line and "SEEN" in line for line in first["pending_messages"])
        receipt = await AgentStore(repo.path).recipient_record(message_hash, receiver)
        assert receipt["first_seen_at"] is not None
        assert receipt["read_at"] is None

        with sqlite3.connect(repo.path) as db:
            old = utc_text(utc_now() - timedelta(seconds=181))
            db.execute(
                "UPDATE coordination_message_recipients SET "
                "first_seen_at=?,last_seen_at=?,seen_count=4 "
                "WHERE message_hash=? AND recipient_agent_id=?",
                (old, old, message_hash, receiver),
            )
            db.commit()
        fifth = await service.read(agent_id=receiver)
        assert any("Read this carefully" in line for line in fifth["pending_messages"])
        sixth = await service.read(agent_id=receiver)
        assert any("message reminder" in line for line in sixth["pending_messages"])

        blocked = await service.run("printf blocked", agent_id=receiver)
        assert blocked["ok"] is False
        assert blocked["coordination_message_pending"] is True

        ack = await service.message(receiver, message_hash=message_hash)
        assert public_agent_name(receiver) in ack["read_by"]
        allowed = await service.run("printf acknowledged", agent_id=receiver, queue_id=1)
        assert allowed["ok"] is True
        completed = await wait_status(service, allowed["cmd_hash"], "completed")
        assert completed["status"] == "completed"
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_require_reply_blocks_run_after_ack_until_linked_reply(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        sender = (await register(service, "Sender", "Coordinate"))["self"]["agent_id"]
        receiver = (await register(service, "Receiver", "Implement"))["self"]["agent_id"]
        sent = await service.message(
            sender,
            target=public_agent_name(receiver),
            text="Confirm before continuing",
            require_reply=True,
        )
        message_hash = sent["message_hash"]
        await service.read(agent_id=receiver)
        await service.message(receiver, message_hash=message_hash)

        blocked = await service.run("printf still-blocked", agent_id=receiver)
        assert blocked["ok"] is False
        assert blocked["reply_required_pending"] is True

        reply = await service.message(receiver, message_hash=message_hash, text="Confirmed")
        assert reply["reply_message_hash"]
        status = await service.message(sender, message_hash=message_hash)
        assert public_agent_name(receiver) in status["replied_by"]

        allowed = await service.run("printf replied", agent_id=receiver, queue_id=1)
        assert allowed["ok"] is True
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_alert_blocks_work_surface_and_keeps_emergency_operations_available(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        sender = (await register(service, "Sender", "Coordinate"))["self"]["agent_id"]
        receiver = (await register(service, "Receiver", "Implement"))["self"]["agent_id"]
        sent = await service.message(
            sender,
            target=public_agent_name(receiver),
            text="Return to chat now",
            alert=True,
        )
        message_hash = sent["message_hash"]

        blocked_read = await service.read(agent_id=receiver)
        assert blocked_read["ok"] is False
        assert blocked_read["alert_pending"] is True
        assert any(
            "ALERT" in line and message_hash in line
            for line in blocked_read["alert_messages"]
        )
        blocked_coordinate = await service.coordinate(receiver)
        assert blocked_coordinate["ok"] is False
        assert blocked_coordinate["alert_pending"] is True

        recovery = await service.recovery("printf emergency-ok", agent_id=receiver)
        assert recovery["ok"] is True
        assert recovery["alert_pending"] is True
        health = await service.health("none", agent_id=receiver)
        assert health["alert_pending"] is True

        await service.message(receiver, message_hash=message_hash, text="Received, returning")
        unblocked = await service.read(agent_id=receiver)
        assert unblocked["ok"] is True
        assert unblocked["alert_pending"] is False
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_absolute_session_warning_and_forced_expiry(tmp_path):
    policy = AgentPolicy(max_session_seconds=1500, session_warning_seconds=180)
    repo, terminal, service = await runtime(tmp_path, policy=policy)
    try:
        agent_id = (await register(service))["self"]["agent_id"]
        with sqlite3.connect(repo.path) as db:
            db.execute(
                "UPDATE agent_sessions SET registered_at=? WHERE agent_id=?",
                (utc_text(utc_now() - timedelta(seconds=1325)), agent_id),
            )
            db.commit()
        health = await service.health("none", agent_id=agent_id)
        assert 0 < health["session_remaining_seconds"] <= 180
        assert "safe checkpoint" in health["session_warning"]
        assert "long build or test run" in health["session_warning"]

        with sqlite3.connect(repo.path) as db:
            db.execute(
                "UPDATE agent_sessions SET registered_at=? WHERE agent_id=?",
                (utc_text(utc_now() - timedelta(seconds=1501)), agent_id),
            )
            db.commit()
        expired = await service.run("printf too-late", agent_id=agent_id)
        assert expired["ok"] is False
        assert expired["registration_required"] is True
        assert expired["session_status"] == "forced"
        assert expired["session_end_reason"] == "max_session_duration"
        observed = await service.agents(target=public_agent_name(agent_id), show_details=True)
        assert observed["sessions"][0]["status"] == "forced"
        assert observed["sessions"][0]["end_reason"] == "max_session_duration"
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_agents_observer_exposes_intents_commands_and_full_command(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent_id = (await register(service, "Journal", "Inspect"))["self"]["agent_id"]
        await service.coordinate(agent_id, step=2, intent="Implement scheduler")
        command_text = "printf 'journal-command\\n'"
        run = await service.run(command_text, agent_id=agent_id, queue_id=2)
        await wait_status(service, run["cmd_hash"], "completed")

        fleet = await service.agents()
        assert any(item["name"] == public_agent_name(agent_id) for item in fleet["sessions"])
        journal = await service.agents(
            target=public_agent_name(agent_id),
            show_details=True,
            show_intents=True,
            show_commands=True,
            since_minutes=60,
        )
        assert journal["sessions"][0]["details"] == ["Inspect", "Implement", "Validate"]
        assert any(item["intent"] == "Implement scheduler" for item in journal["intent_journal"])
        assert journal["command_journal"][0]["command_hash"] == run["cmd_hash"]
        assert journal["command_journal"][0]["queue_id"] == 2

        detail = await service.agents(command_hash=run["cmd_hash"])
        assert detail["command"]["cmd"] == command_text
        assert detail["command"]["agent_name"] == public_agent_name(agent_id)
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_queue_affinity_parallelism_and_command_survival_after_finish(tmp_path):
    _, terminal, service = await runtime(tmp_path, workers=2)
    try:
        first_agent = (await register(service, "First", "Queue one"))["self"]["agent_id"]
        second_agent = (await register(service, "Second", "Queue two"))["self"]["agent_id"]

        release_slow = tmp_path / "release-slow"
        slow = await service.run(
            "while [ ! -f release-slow ]; do sleep 0.02; done; printf slow",
            agent_id=first_agent,
            queue_id=2,
        )
        for _ in range(100):
            if (await service.read(slow["cmd_hash"]))["status"] == "running":
                break
            await asyncio.sleep(0.005)
        inherited = await service.run("printf inherited", agent_id=first_agent)
        assert inherited["queue_id"] == 2

        fast = await service.run("printf fast", agent_id=second_agent, queue_id=1)
        fast_done = await wait_status(service, fast["cmd_hash"], "completed")
        assert fast_done["status"] == "completed"
        assert (await service.read(slow["cmd_hash"]))["status"] == "running"

        await service.agent_finish(first_agent)
        release_slow.write_text("go")
        slow_done = await wait_status(service, slow["cmd_hash"], "completed")
        inherited_done = await wait_status(service, inherited["cmd_hash"], "completed")
        assert slow_done["status"] == "completed"
        assert inherited_done["status"] == "completed"
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_queue_reconciliation_claims_persisted_work_without_submit_signal(tmp_path):
    repo, terminal, service = await runtime(tmp_path, workers=1, reconcile=0.03)
    try:
        command = await repo.create(
            "printf reconciled",
            status="queued",
            agent_id="anonymous",
            command_type="run",
            command_preview="printf reconciled",
            queue_id=1,
        )
        await terminal.start()
        result = await wait_status(service, command.cmd_hash, "completed")
        assert result["status"] == "completed"
        assert result["queue_id"] == 1
    finally:
        await terminal.stop()


def test_v07_database_migrates_to_v08_without_reset(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE commands(
                hash TEXT PRIMARY KEY, cmd TEXT, status TEXT, pid INTEGER,
                exit_code INTEGER, error TEXT, started_at TEXT, finished_at TEXT
            );
            CREATE TABLE agent_sessions(
                agent_id TEXT PRIMARY KEY, registered_at TEXT NOT NULL,
                last_activity_at TEXT NOT NULL, task_summary TEXT NOT NULL,
                intent TEXT NOT NULL, work_scope TEXT NOT NULL, state TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '[]', current_step INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE agent_task_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,
                timestamp TEXT NOT NULL, intent TEXT NOT NULL, work_scope TEXT NOT NULL,
                step INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE coordination_messages(
                message_hash TEXT PRIMARY KEY, sender_agent_id TEXT NOT NULL,
                target_name TEXT, text TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE coordination_message_recipients(
                message_hash TEXT NOT NULL, recipient_agent_id TEXT NOT NULL, read_at TEXT,
                PRIMARY KEY(message_hash, recipient_agent_id)
            );
            INSERT INTO commands(hash,cmd,status) VALUES('deadbeef','printf old','completed');
            """
        )
        db.commit()

    async def migrate():
        repo = SqliteRepository(path)
        await repo.initialize()

    asyncio.run(migrate())
    with sqlite3.connect(path) as db:
        command_columns = {row[1] for row in db.execute("PRAGMA table_info(commands)")}
        session_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_sessions)")}
        message_columns = {row[1] for row in db.execute("PRAGMA table_info(coordination_messages)")}
        recipient_columns = {
            row[1] for row in db.execute("PRAGMA table_info(coordination_message_recipients)")
        }
        assert {"queue_id", "queue_sequence", "enqueued_at", "claimed_at"} <= command_columns
        assert {"ended_at", "end_reason", "preferred_queue_id"} <= session_columns
        assert {"require_reply", "alert"} <= message_columns
        assert {"delivered_at", "first_seen_at", "seen_count", "replied_at"} <= recipient_columns
        old_command = db.execute(
            "SELECT cmd FROM commands WHERE hash='deadbeef'"
        ).fetchone()[0]
        assert old_command == "printf old"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4


@pytest.mark.asyncio
async def test_message_tool_response_counts_as_seen_and_observer_tracks_receipt_states(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    try:
        sender = (await register(service, "Sender", "Coordinate"))["self"]["agent_id"]
        receiver = (await register(service, "Receiver", "Implement"))["self"]["agent_id"]
        sent = await service.message(
            sender,
            target=public_agent_name(receiver),
            text="Acknowledge and answer",
            require_reply=True,
        )
        message_hash = sent["message_hash"]

        delivered = await service.agents(target=public_agent_name(receiver))
        entry = next(
            item for item in delivered["sessions"][0]["message_journal"]
            if item["message_hash"] == message_hash
        )
        assert entry["state"] == "delivered"

        side_note = await service.message(
            receiver,
            target=public_agent_name(sender),
            text="Working on it",
        )
        assert side_note["ok"] is True
        receipt = await AgentStore(repo.path).recipient_record(message_hash, receiver)
        assert receipt["seen_count"] == 1
        assert receipt["read_at"] is None

        seen = await service.agents(target=public_agent_name(receiver))
        record = seen["sessions"][0]
        entry = next(
            item for item in record["message_journal"] if item["message_hash"] == message_hash
        )
        assert entry["state"] == "seen"
        assert record["last_activity_tool"] == "message"

        await service.message(receiver, message_hash=message_hash)
        acknowledged = await service.agents(target=public_agent_name(receiver))
        entry = next(
            item for item in acknowledged["sessions"][0]["message_journal"]
            if item["message_hash"] == message_hash
        )
        assert entry["state"] == "read"
        assert acknowledged["sessions"][0]["messages_awaiting_reply"] == 1

        await service.message(receiver, message_hash=message_hash, text="Confirmed")
        replied = await service.agents(target=public_agent_name(receiver))
        entry = next(
            item for item in replied["sessions"][0]["message_journal"]
            if item["message_hash"] == message_hash
        )
        assert entry["state"] == "replied"
        assert replied["sessions"][0]["messages_awaiting_reply"] == 0
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_global_read_uses_batch_queue_lookup(tmp_path, monkeypatch):
    repo, terminal, service = await runtime(tmp_path, workers=1)
    try:
        agent_id = (await register(service, "Reader", "Generate output"))["self"]["agent_id"]
        submitted = await service.run("printf 'batch-queue-map\\n'", agent_id=agent_id, queue_id=1)
        completed = await wait_status(service, submitted["cmd_hash"], "completed")
        assert completed["status"] == "completed"

        async def unexpected_get(_cmd_hash):
            raise AssertionError(
                "global read must use batch queue lookup instead of per-command get"
            )

        monkeypatch.setattr(repo, "get", unexpected_get)
        global_read = await service.read(lines_count=100)
        assert global_read["ok"] is True
        assert any(" q1 batch-queue-map" in line for line in global_read["lines"])
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_cancel_after_claim_before_spawn_cannot_resurrect_command(tmp_path, monkeypatch):
    repo, terminal, service = await runtime(tmp_path, workers=1, reconcile=0.01)
    release_spawn = asyncio.Event()
    real_spawn = terminal._spawn

    async def delayed_spawn():
        await release_spawn.wait()
        return await real_spawn()

    monkeypatch.setattr(terminal, "_spawn", delayed_spawn)
    try:
        await terminal.start()
        command = await repo.create(
            "printf 'must-not-run\\n'",
            status="queued",
            agent_id="anonymous",
            command_type="run",
            command_preview="printf must-not-run",
            queue_id=1,
        )
        await terminal.submit(command)

        claimed = await wait_status(service, command.cmd_hash, "running")
        assert claimed["status"] == "running"
        running = await repo.get(command.cmd_hash)
        cancelled, error = await terminal.cancel(running, timeout_seconds=0.05)
        assert cancelled is True
        assert error is None

        release_spawn.set()
        for _ in range(100):
            current = await service.read(command.cmd_hash, 100, 0)
            if current["status"] == "cancelled" and command.cmd_hash not in terminal.processes:
                break
            await asyncio.sleep(0.01)

        current = await service.read(command.cmd_hash, 100, 0)
        assert current["status"] == "cancelled"
        assert current["lines"] == []
        health = await terminal.health()
        assert health["queues"][0]["queued"] == 0
        assert health["queues"][0]["running"] is None
    finally:
        release_spawn.set()
        await terminal.stop()
