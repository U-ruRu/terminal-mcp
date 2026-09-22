import asyncio
import re
import sqlite3
from datetime import timedelta

import pytest

import terminal_mcp.core.agents as agents_module
from terminal_mcp.core.orchestration import (
    CROCKFORD,
    NATO_WORDS,
    find_scope_overlaps,
    generate_agent_id,
    generate_suffix,
    normalize_preview,
    public_agent_name,
    scopes_overlap,
    utc_now,
    utc_text,
)
from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


async def runtime(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000)
    return repo, terminal, service


async def register(service, summary, intent, work_scope=None, details=None):
    return await service.agent_start(
        task_summary=summary,
        intent=intent,
        details=details or [intent],
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


def test_call_sign_and_preview_helpers():
    suffix_only = generate_suffix()
    assert len(suffix_only) == 4 and all(ch in CROCKFORD for ch in suffix_only)
    agent_id = generate_agent_id()
    word, suffix = agent_id.rsplit("-", 1)
    assert word in NATO_WORDS
    assert len(suffix) == 4 and all(ch in CROCKFORD for ch in suffix)
    assert re.fullmatch(r"[A-Za-z-]+-[0-9A-HJKMNP-TV-Z]{4}", agent_id)
    assert normalize_preview("  printf   hello\n world  ") == "printf hello world"
    assert len(normalize_preview("x" * 150)) == 100


def test_hierarchical_scope_matching():
    assert scopes_overlap("repo:src/terminal_mcp", "repo:src/terminal_mcp/storage")
    assert scopes_overlap("repo:storage", "repo:storage")
    assert not scopes_overlap("repo:storage", "repo:mcp")
    sessions = [{"agent_id": "Bravo-1234", "work_scope": ["repo:src/terminal_mcp/storage"]}]
    assert find_scope_overlaps(["repo:src/terminal_mcp"], sessions) == [
        {"name": "Bravo", "scope": "repo:src/terminal_mcp/storage"}
    ]


@pytest.mark.asyncio
async def test_session_uniqueness_collision_retry_and_task_history(tmp_path, monkeypatch):
    repo, terminal, service = await runtime(tmp_path)
    store = AgentStore(repo.path)
    now = utc_text()
    await store.create_session("Alpha-1111", "one", "first", ["repo:storage"], ["first"], 1, now)
    with pytest.raises(sqlite3.IntegrityError):
        await store.create_session("Alpha-1111", "two", "second", ["repo:mcp"], ["second"], 1, now)

    ids = iter(["Alpha-9999", "Bravo-2222"])
    monkeypatch.setattr(agents_module, "generate_agent_id", lambda: next(ids))
    created = await register(service, "Implement registry", "Inspect schema", ["repo:storage"])
    assert created["self"]["agent_id"] == "Bravo-2222"
    updated = await service.coordinate("Bravo-2222", step=1, intent="Edit schema")
    assert updated["agent_name"] == "Bravo"
    assert updated["intent"] == "Edit schema"
    assert updated["step"] == 1
    overview = await service.agents("Bravo-2222")
    assert overview["self"]["work_scope"] == ["repo:storage"]
    with sqlite3.connect(repo.path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM agent_task_events WHERE agent_id='Bravo-2222'"
        ).fetchone()[0]
    assert count == 2
    await terminal.stop()


@pytest.mark.asyncio
async def test_activity_refresh_expiry_and_reregistration(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    started = await register(service, "TTL test", "Observe", ["repo:tests"])
    agent_id = started["self"]["agent_id"]
    before = (await AgentStore(repo.path).get_session(agent_id))["last_activity_at"]
    await terminal.start()
    await asyncio.sleep(0.01)
    health = await service.health("none")
    assert health["ok"] is True
    after = (await AgentStore(repo.path).get_session(agent_id))["last_activity_at"]
    assert after == before

    service.agent_coordinator.ttl_seconds = 0.01
    await asyncio.sleep(0.03)
    expired = await service.run("printf stale", agent_id=agent_id)
    assert expired["session_expired"] is True
    assert expired["registration_required"] is True
    fresh = await register(service, "TTL test", "Continue", ["repo:tests"])
    assert fresh["self"]["agent_id"] != agent_id
    await terminal.stop()


@pytest.mark.asyncio
async def test_command_attribution_recent_order_and_global_read(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    started = await register(service, "Attribution", "Run commands", ["repo:tests"])
    agent_id = started["self"]["agent_id"]
    first = await service.run("printf 'one\\n'", agent_id=agent_id)
    await wait_finished(service, agent_id, first["cmd_hash"])
    second = await service.recovery("printf 'two\\n'", agent_id=agent_id)
    recent = await AgentStore(repo.path).recent_commands(agent_id, 3)
    assert [item["command_hash"] for item in recent[:2]] == [second["cmd_hash"], first["cmd_hash"]]
    assert recent[0]["preview"] == "printf 'two\\n'"
    global_read = await service.read(None, 20, 0)
    agent_name = public_agent_name(agent_id)
    assert any(f"{agent_name} {first['cmd_hash']}" in line for line in global_read["lines"])
    assert any(f"{agent_name} {second['cmd_hash']}" in line for line in global_read["lines"])
    assert all(agent_id not in line for line in global_read["lines"])
    busy = await service.run("sleep 30", agent_id=agent_id)
    await asyncio.sleep(0.03)
    cancelled = await service.cancel(busy["cmd_hash"], agent_id=agent_id)
    assert cancelled["ok"] is True
    assert (await service.read(busy["cmd_hash"], agent_id=agent_id))["status"] == "cancelled"
    await terminal.stop()


@pytest.mark.asyncio
async def test_multi_agent_overlap_compact_limits_and_persistence(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    a = await register(service,
        "Implement storage changes", "Edit SQLite schema", ["repo:storage"]
    )
    aid = a["self"]["agent_id"]
    command = await service.run("printf 'agent-a\\n'", agent_id=aid)
    await wait_finished(service, aid, command["cmd_hash"])

    b = await register(service, "Review MCP", "Inspect active agent", ["repo:mcp"])
    bid = b["self"]["agent_id"]
    seen = next(item for item in b["active"] if item["name"] == public_agent_name(aid))
    assert seen["intent"] == "Edit SQLite schema"
    assert seen["recent_commands"][0]["command_hash"] == command["cmd_hash"]
    overlap = await service.coordinate(bid, step=1, intent="Edit storage adapter")
    assert overlap["ok"] is True
    b_overview = await service.agents(bid)
    assert b_overview["overlaps"] == []
    assert b_overview["self"]["work_scope"] == ["repo:mcp"]

    global_read = await service.read(None, 20, 0)
    assert any(
        f"{public_agent_name(aid)} {command['cmd_hash']}" in line
        for line in global_read["lines"]
    )
    assert len(b_overview["active"]) <= 8
    assert all(len(item["recent_commands"]) <= 3 for item in b_overview["active"])

    await terminal.stop()
    repo2 = SqliteRepository(repo.path)
    await repo2.initialize()
    terminal2 = LinuxTerminalAdapter(repo2, "/bin/bash", tmp_path, 0.1)
    service2 = TerminalService(repo2, terminal2, 5000)
    persisted = await service2.agents(bid)
    assert persisted["ok"] is True
    assert persisted["self"]["name"] == public_agent_name(bid)
    assert "agent_id" not in persisted["self"]
    await terminal2.stop()


@pytest.mark.asyncio
async def test_active_filtering_and_finish(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    started = await register(service, "Finish", "Finish session", ["repo:tests"])
    agent_id = started["self"]["agent_id"]
    old = utc_text(utc_now() - timedelta(seconds=600))
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE agent_sessions SET last_activity_at=? WHERE agent_id=?", (old, agent_id))
        db.commit()
    service.agent_coordinator.ttl_seconds = 300
    expired = await service.agents(agent_id)
    assert expired["registration_required"] is True

    fresh = await register(service, "Finish", "Finish session", ["repo:tests"])
    fid = fresh["self"]["agent_id"]
    finished = await service.agent_finish(fid)
    assert finished["ok"] is True
    assert finished["agent_name"] == public_agent_name(fid)
    assert finished["finished"] is True
    assert finished["pending_messages"] == []
    assert (await service.agents(fid))["registration_required"] is True
    await terminal.stop()


@pytest.mark.asyncio
async def test_active_overview_enforces_eight_agent_limit(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    caller = await register(service, "Caller", "Observe peers", ["repo:tests"])
    caller_id = caller["self"]["agent_id"]
    for index in range(10):
        await register(service, f"Peer {index}", f"Task {index}", [f"repo:peer/{index}"])
    overview = await service.agents(caller_id)
    assert len(overview["active"]) == 8
    assert overview["additional_active_agents"] == 2
    await terminal.stop()

@pytest.mark.asyncio
async def test_run_requires_fresh_task_lease_and_coordinate_refreshes_it(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    started = await register(service, "Lease test", "Initial task", ["repo:tests"])
    agent_id = started["self"]["agent_id"]
    assert started["self"]["task_lease_seconds"] == 180
    service.agent_coordinator.task_lease_seconds = 0.01
    await asyncio.sleep(0.03)
    blocked = await service.run("printf stale", agent_id=agent_id)
    assert blocked["ok"] is False
    assert blocked["task_context_expired"] is True
    assert blocked["max_task_age_seconds"] == 0.01
    assert "coordinate" in blocked["error"]
    service.agent_coordinator.task_lease_seconds = 1
    refreshed = await service.coordinate(agent_id, step=1, intent="Refreshed task")
    assert refreshed["ok"] is True
    submitted = await service.run("printf 'fresh\\n'", agent_id=agent_id)
    assert submitted["ok"] is True
    finished = await wait_finished(service, agent_id, submitted["cmd_hash"])
    assert finished["status"] == "completed"
    await terminal.stop()

@pytest.mark.asyncio
async def test_anonymous_command_attribution_is_persisted(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    result = await service.recovery("printf 'anonymous-recovery\\n'")
    assert result["agent_name"] == "anonymous"
    global_read = await service.read(None, 20, 0)
    assert any(f"anonymous {result['cmd_hash']}" in line for line in global_read["lines"])
    with sqlite3.connect(repo.path) as db:
        owner = db.execute(
            "SELECT agent_id FROM command_agent_attribution WHERE command_hash=?",
            (result["cmd_hash"],),
        ).fetchone()[0]
    assert owner == "anonymous"
    await terminal.stop()


@pytest.mark.asyncio
async def test_run_and_read_expose_public_active_agent_awareness(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    first = await register(service, "First", "Observe peer", ["repo:first"])
    first_id = first["self"]["agent_id"]
    second = await register(service, "Second", "Run peer command", ["repo:second"])
    second_id = second["self"]["agent_id"]
    second_name = public_agent_name(second_id)

    first_command = await service.run("printf 'first\n'", agent_id=first_id)
    assert any(
        f"{second_name} started — Run peer command" in line
        for line in first_command["active_agents"]
    )
    await wait_finished(service, first_id, first_command["cmd_hash"])

    peer_run = await service.run("sleep 0.2; printf 'peer\n'", agent_id=second_id)
    for _ in range(100):
        peer_command = await repo.get(peer_run["cmd_hash"])
        if peer_command and peer_command.status == "running":
            break
        await asyncio.sleep(0.01)
    assert peer_command.status == "running"

    own_run = await service.run("printf 'own\n'", agent_id=first_id)
    peers = own_run["active_agents"]
    assert any(f"{second_name} {peer_run['cmd_hash']} — Run peer command" in line for line in peers)
    assert not any(f"{second_name} started" in line for line in peers)
    assert all(" ago " in line or line.startswith("yesterday ") for line in peers)
    assert second_id not in str(peers)
    assert "sleep 0.2" not in str(peers)

    own_read = await service.read(first_command["cmd_hash"], agent_id=first_id)
    assert own_read["agent_name"] == public_agent_name(first_id)
    assert any(
        f"{second_name} {peer_run['cmd_hash']} — Run peer command" in line
        for line in own_read["active_agents"]
    )

    await wait_finished(service, second_id, peer_run["cmd_hash"])
    finished_read = await service.read(first_command["cmd_hash"], agent_id=first_id)
    assert any(
        f"{second_name} {peer_run['cmd_hash']} — Run peer command" in line
        for line in finished_read["active_agents"]
    )

    await service.agent_finish(second_id)
    lifecycle_read = await service.read(first_command["cmd_hash"], agent_id=first_id)
    assert any(
        f"{second_name} finished — Run peer command" in line
        for line in lifecycle_read["active_agents"]
    )

    await wait_finished(service, first_id, own_run["cmd_hash"])
    await terminal.stop()

@pytest.mark.asyncio
async def test_agent_actions_refresh_session_not_task_lease(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    started = await register(service, "TTL refresh", "Initial intent", ["repo:tests"])
    agent_id = started["self"]["agent_id"]
    store = AgentStore(repo.path)

    session0 = await store.get_session(agent_id)
    task0 = await store.latest_task_at(agent_id)

    await asyncio.sleep(0.01)
    overview = await service.agents(agent_id)
    assert overview["ok"] is True
    session1 = await store.get_session(agent_id)
    assert session1["last_activity_at"] > session0["last_activity_at"]
    assert await store.latest_task_at(agent_id) == task0

    await asyncio.sleep(0.01)
    missing = await service.read("deadbeef", agent_id=agent_id)
    assert missing["status"] == "not_found"
    session2 = await store.get_session(agent_id)
    assert session2["last_activity_at"] > session1["last_activity_at"]
    assert await store.latest_task_at(agent_id) == task0

    await asyncio.sleep(0.01)
    recovery = await service.recovery("printf 'ttl-recovery\n'", agent_id=agent_id)
    assert recovery["ok"] is True
    session3 = await store.get_session(agent_id)
    assert session3["last_activity_at"] > session2["last_activity_at"]
    assert await store.latest_task_at(agent_id) == task0

    await asyncio.sleep(0.01)
    cancelled = await service.cancel("deadbeef", agent_id=agent_id)
    assert cancelled["ok"] is False
    session4 = await store.get_session(agent_id)
    assert session4["last_activity_at"] > session3["last_activity_at"]
    assert await store.latest_task_at(agent_id) == task0

    await asyncio.sleep(0.01)
    task = await service.coordinate(agent_id, step=1, intent="New short intent")
    assert task["ok"] is True
    session5 = await store.get_session(agent_id)
    task5 = await store.latest_task_at(agent_id)
    assert session5["last_activity_at"] > session4["last_activity_at"]
    assert task5 > task0
    await terminal.stop()


@pytest.mark.asyncio
async def test_public_name_is_reserved_for_full_session_ttl_after_finish(tmp_path, monkeypatch):
    repo, terminal, service = await runtime(tmp_path)
    ids = iter(["India-1111", "India-2222", "Juliett-3333"])
    monkeypatch.setattr(agents_module, "generate_agent_id", lambda: next(ids))

    first = await register(service, "First", "Work", ["repo:first"])
    assert first["self"]["agent_id"] == "India-1111"
    assert (await service.agent_finish("India-1111"))["finished"] is True

    second = await register(service, "Second", "Work", ["repo:second"])
    assert second["self"]["agent_id"] == "Juliett-3333"
    assert second["self"]["name"] == "Juliett"
    await terminal.stop()


@pytest.mark.asyncio
async def test_agent_plan_coordinate_and_agent_start_update(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    missing_plan = await service.agent_start(
        task_summary="Missing plan",
        intent="Cannot register",
    )
    assert missing_plan["ok"] is False
    assert "details" in missing_plan["error"]

    started = await register(
        service,
        "Plan test",
        "Inspect current state",
        details=["Inspect current state", "Patch implementation", "Run tests"],
    )
    agent_id = started["self"]["agent_id"]
    no_op = await service.agent_start(agent_id=agent_id)
    assert no_op["ok"] is False
    assert "at least one field" in no_op["error"]
    assert started["self"]["work_scope"] == []
    assert started["self"]["details"] == [
        "Inspect current state",
        "Patch implementation",
        "Run tests",
    ]
    assert started["self"]["current_step"] == 1

    inspected = await service.coordinate(agent_id, step=2)
    assert inspected["ok"] is True
    assert inspected["step"] == 2
    assert inspected["detail"] == "Patch implementation"
    assert inspected["intent"] == "Inspect current state"

    updated = await service.coordinate(agent_id, step=2, intent="Patching implementation")
    assert updated["ok"] is True
    assert updated["step"] == 2
    assert updated["intent"] == "Patching implementation"
    overview = await service.agents(agent_id)
    assert overview["self"]["current_step"] == 2

    intent_only = await service.agent_start(
        agent_id=agent_id,
        intent="Small plan-preserving correction",
    )
    assert intent_only["ok"] is True
    assert intent_only["self"]["details"] == [
        "Inspect current state",
        "Patch implementation",
        "Run tests",
    ]

    replanned = await service.agent_start(
        agent_id=agent_id,
        details=["Re-check design", "Finish implementation", "Regression"],
    )
    assert replanned["ok"] is True
    assert replanned["self"]["name"] == public_agent_name(agent_id)
    assert "agent_id" not in replanned["self"]
    assert replanned["self"]["details"][0] == "Re-check design"
    assert replanned["self"]["current_step"] == 2
    await terminal.stop()


@pytest.mark.asyncio
async def test_coordinate_show_details_exposes_other_agent_plan(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    first = await register(
        service,
        "First",
        "Work on API",
        details=["Inspect API", "Patch API"],
    )
    second = await register(
        service,
        "Second",
        "Work on storage",
        details=["Inspect storage", "Patch storage", "Test storage"],
    )
    first_id = first["self"]["agent_id"]
    second_id = second["self"]["agent_id"]
    await service.coordinate(second_id, step=2, intent="Patching storage")

    result = await service.coordinate(first_id, show_details=True)
    second_name = public_agent_name(second_id)
    assert any(
        f"{second_name} step 2/3 — Patching storage" in line
        and "1. Inspect storage" in line
        and "3. Test storage" in line
        for line in result["other_details"]
    )
    await terminal.stop()


@pytest.mark.asyncio
async def test_direct_message_delivery_blocks_run_until_ack(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    sender = await register(service, "Sender", "Coordinate", details=["Coordinate", "Test"])
    receiver = await register(service, "Receiver", "Implement", details=["Implement", "Test"])
    sender_id = sender["self"]["agent_id"]
    receiver_id = receiver["self"]["agent_id"]
    receiver_name = public_agent_name(receiver_id)
    sender_name = public_agent_name(sender_id)

    sent = await service.message(
        sender_id,
        text="Storage is mine; take the MCP contract.",
        target=receiver_name,
    )
    assert sent["ok"] is True
    assert sent["delivered_to"] == [receiver_name]
    assert re.fullmatch(r"[0-9a-f]{8}", sent["message_hash"])
    message_hash = sent["message_hash"]

    read = await service.read("deadbeef", agent_id=receiver_id)
    assert any(
        message_hash in line
        and sender_name in line
        and "→ you:" in line
        and f"ack: message({message_hash})" in line
        for line in read["pending_messages"]
    )

    health = await service.health("none", agent_id=receiver_id)
    assert any(message_hash in line for line in health["pending_messages"])

    recovery = await service.recovery("printf 'emergency\n'", agent_id=receiver_id)
    assert recovery["ok"] is True
    assert any(message_hash in line for line in recovery["pending_messages"])

    blocked = await service.run("printf 'must-not-run\n'", agent_id=receiver_id)
    assert blocked["ok"] is False
    assert blocked["coordination_message_pending"] is True
    assert blocked["cmd_hash"] is None
    assert any(message_hash in line for line in blocked["pending_messages"])

    ack = await service.message(receiver_id, message_hash=message_hash)
    assert ack["ok"] is True
    assert ack["read_by"] == [receiver_name]
    assert ack["pending_messages"] == []

    submitted = await service.run("printf 'after-ack\n'", agent_id=receiver_id)
    assert submitted["ok"] is True
    await wait_finished(service, receiver_id, submitted["cmd_hash"])
    await terminal.stop()


@pytest.mark.asyncio
async def test_broadcast_message_snapshots_recipients_and_receipts(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    sender = await register(service, "Sender", "Broadcast", details=["Broadcast"])
    one = await register(service, "One", "Listen", details=["Listen"])
    two = await register(service, "Two", "Listen", details=["Listen"])
    sender_id = sender["self"]["agent_id"]
    one_id = one["self"]["agent_id"]
    two_id = two["self"]["agent_id"]

    sent = await service.message(sender_id, text="Shared coordination note")
    assert sent["ok"] is True
    assert set(sent["delivered_to"]) == {
        public_agent_name(one_id),
        public_agent_name(two_id),
    }

    later = await register(service, "Later", "Joined later", details=["Joined later"])
    later_id = later["self"]["agent_id"]
    later_read = await service.read("deadbeef", agent_id=later_id)
    assert sent["message_hash"] not in str(later_read["pending_messages"])

    sender_status = await service.message(sender_id, message_hash=sent["message_hash"])
    assert sender_status["ok"] is True
    assert sender_status["read_by"] == []

    ack = await service.message(one_id, message_hash=sent["message_hash"])
    assert ack["read_by"] == [public_agent_name(one_id)]
    sender_status = await service.message(sender_id, message_hash=sent["message_hash"])
    assert sender_status["read_by"] == [public_agent_name(one_id)]
    two_read = await service.read("deadbeef", agent_id=two_id)
    assert any(
        sent["message_hash"] in line and "→ all:" in line
        for line in two_read["pending_messages"]
    )

    invalid = await service.message(
        sender_id,
        text="Wrong target format",
        target=one_id,
    )
    assert invalid["ok"] is False
    assert "public agent name" in invalid["error"]
    await terminal.stop()


@pytest.mark.asyncio
async def test_pending_message_is_in_all_agent_bound_coordination_responses(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    sender = await register(service, "Sender", "Coordinate", details=["Coordinate"])
    receiver = await register(
        service,
        "Receiver",
        "Implement",
        details=["Implement", "Verify"],
    )
    sender_id = sender["self"]["agent_id"]
    receiver_id = receiver["self"]["agent_id"]
    sent = await service.message(
        sender_id,
        text="Please coordinate before changing storage.",
        target=public_agent_name(receiver_id),
    )
    message_hash = sent["message_hash"]

    coordinate = await service.coordinate(receiver_id)
    assert any(message_hash in line for line in coordinate["pending_messages"])

    overview = await service.agents(receiver_id)
    assert any(message_hash in line for line in overview["pending_messages"])

    cancelled = await service.cancel("deadbeef", agent_id=receiver_id)
    assert any(message_hash in line for line in cancelled["pending_messages"])

    updated_start = await service.agent_start(
        agent_id=receiver_id,
        intent="Still coordinating",
    )
    assert any(message_hash in line for line in updated_start["pending_messages"])

    own_message = await service.message(
        receiver_id,
        text="I am coordinating.",
        target=public_agent_name(sender_id),
    )
    assert any(message_hash in line for line in own_message["pending_messages"])

    finished = await service.agent_finish(receiver_id)
    assert any(message_hash in line for line in finished["pending_messages"])
    await terminal.stop()
