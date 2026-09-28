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
    session_expiry_reason,
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


def test_call_sign_and_preview_helpers():
    suffix_only = generate_suffix()
    assert len(suffix_only) == 8 and all(ch in CROCKFORD for ch in suffix_only)
    agent_id = generate_agent_id()
    word, suffix = agent_id.rsplit("-", 1)
    assert word in NATO_WORDS
    assert len(suffix) == 8 and all(ch in CROCKFORD for ch in suffix)
    assert re.fullmatch(r"[A-Za-z-]+-[0-9A-HJKMNP-TV-Z]{8}", agent_id)
    assert normalize_preview("  printf   hello\n world  ") == "printf hello world"
    assert len(normalize_preview("x" * 150)) == 100


def test_global_expiry_is_authoritative_when_local_max_differs():
    now = utc_now()
    session = {
        "state": "active",
        "registered_at": utc_text(now - timedelta(seconds=1200)),
        "last_activity_at": utc_text(now - timedelta(seconds=1)),
        "global_expires_at": utc_text(now + timedelta(seconds=300)),
    }

    assert (
        session_expiry_reason(
            session,
            now=now,
            idle_ttl_seconds=300,
            max_session_seconds=600,
        )
        is None
    )
    assert (
        session_expiry_reason(
            session,
            now=now + timedelta(seconds=301),
            idle_ttl_seconds=1000,
            max_session_seconds=600,
        )
        == "max_session_duration"
    )


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

    ids = iter(["Alpha-99999999", "Bravo-22222222"])
    monkeypatch.setattr(agents_module, "generate_agent_id", lambda: next(ids))
    created = await register(service, "Implement registry", "Inspect schema", ["repo:storage"])
    assert created["self"]["agent_id"] == "Bravo-22222222"
    updated = await service.coordinate("Bravo-22222222", step=1, intent="Edit schema")
    assert updated["agent_name"] == "Bravo"
    assert updated["intent"] == "Edit schema"
    assert updated["step"] == 1
    overview = await service.agents("Bravo-22222222")
    assert overview["self"]["work_scope"] == ["repo:storage"]
    with sqlite3.connect(repo.path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM agent_task_events WHERE agent_id='Bravo-22222222'"
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
    expired = await service.run("printf stale", agent_id=agent_id, task_scope="none")
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
    first = await service.run("printf 'one\\n'", agent_id=agent_id, task_scope="none")
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
    busy = await service.run("sleep 30", agent_id=agent_id, task_scope="none")
    await asyncio.sleep(0.03)
    cancelled = await service.cancel(busy["cmd_hash"], agent_id=agent_id)
    assert cancelled["ok"] is True
    assert (await service.read(busy["cmd_hash"], agent_id=agent_id))["status"] == "cancelled"
    await terminal.stop()


@pytest.mark.asyncio
async def test_multi_agent_overlap_compact_limits_and_persistence(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    a = await register(service, "Implement storage changes", "Edit SQLite schema", ["repo:storage"])
    aid = a["self"]["agent_id"]
    command = await service.run("printf 'agent-a\\n'", agent_id=aid, task_scope="none")
    await wait_finished(service, aid, command["cmd_hash"])

    b = await register(service, "Review MCP", "Inspect active agent", ["repo:mcp"])
    bid = b["self"]["agent_id"]
    seen = next(item for item in b["active"] if item["name"] == public_agent_name(aid))
    assert seen["intent"] == "Edit SQLite schema"
    assert "recent_commands" not in seen
    with_commands = await service.agents(bid, show_commands=True)
    seen_commands = next(
        item for item in with_commands["active"] if item["name"] == public_agent_name(aid)
    )
    assert seen_commands["recent_commands"][0]["command_hash"] == command["cmd_hash"]
    overlap = await service.coordinate(bid, step=1, intent="Edit storage adapter")
    assert overlap["ok"] is True
    b_overview = await service.agents(bid)
    assert b_overview["overlaps"] is None
    assert b_overview["self"]["work_scope"] == ["repo:mcp"]

    global_read = await service.read(None, 20, 0)
    assert any(
        f"{public_agent_name(aid)} {command['cmd_hash']}" in line for line in global_read["lines"]
    )
    assert len(b_overview["active"]) <= 8
    assert all("recent_commands" not in item for item in b_overview["active"])
    detailed_commands = await service.agents(bid, show_commands=True)
    assert all(len(item.get("recent_commands", [])) <= 3 for item in detailed_commands["active"])

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
    assert finished.get("pending_messages", []) == []
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
    blocked = await service.run("printf stale", agent_id=agent_id, task_scope="none")
    assert blocked["ok"] is False
    assert blocked["task_context_expired"] is True
    assert blocked["max_task_age_seconds"] == 0.01
    assert "coordinate" in blocked["error"]
    service.agent_coordinator.task_lease_seconds = 1
    refreshed = await service.coordinate(agent_id, step=1, intent="Refreshed task")
    assert refreshed["ok"] is True
    submitted = await service.run("printf 'fresh\\n'", agent_id=agent_id, task_scope="none")
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
async def test_run_and_read_use_compact_context_and_agents_exposes_awareness(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    first = await register(service, "First", "Observe peer", ["repo:first"])
    first_id = first["self"]["agent_id"]
    second = await register(service, "Second", "Run peer command", ["repo:second"])
    second_id = second["self"]["agent_id"]
    second_name = public_agent_name(second_id)

    first_command = await service.run("printf 'first\n'", agent_id=first_id, task_scope="none")
    assert "active_agents" not in first_command
    overview = await service.agents(first_id)
    peer = next(item for item in overview["active"] if item["name"] == second_name)
    assert peer["intent"] == "Run peer command"
    assert second_id not in str(peer)
    await wait_finished(service, first_id, first_command["cmd_hash"])

    peer_run = await service.run(
        "sleep 0.2; printf 'peer\n'", agent_id=second_id, task_scope="none"
    )
    for _ in range(100):
        peer_command = await repo.get(peer_run["cmd_hash"])
        if peer_command and peer_command.status == "running":
            break
        await asyncio.sleep(0.01)
    assert peer_command.status == "running"

    own_run = await service.run("printf 'own\n'", agent_id=first_id, task_scope="none")
    assert "active_agents" not in own_run
    command_overview = await service.agents(first_id, show_commands=True)
    peer = next(item for item in command_overview["active"] if item["name"] == second_name)
    assert peer["recent_commands"][0]["command_hash"] == peer_run["cmd_hash"]
    assert second_id not in str(peer)
    assert peer["recent_commands"][0]["preview"].startswith("sleep 0.2")

    own_read = await service.read(first_command["cmd_hash"], agent_id=first_id)
    assert "active_agents" not in own_read

    await wait_finished(service, second_id, peer_run["cmd_hash"])
    finished_overview = await service.agents(first_id, show_commands=True)
    peer = next(item for item in finished_overview["active"] if item["name"] == second_name)
    assert peer["recent_commands"][0]["command_hash"] == peer_run["cmd_hash"]
    assert peer["recent_commands"][0]["status"] == "completed"

    await service.agent_finish(second_id)
    lifecycle = await service.agents(first_id, target=second_name, show_commands=True)
    assert lifecycle["sessions"][0]["status"] == "finished"
    assert lifecycle["sessions"][0]["name"] == second_name

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
async def test_public_name_is_reserved_for_180_seconds_after_finished_session(
    tmp_path, monkeypatch
):
    repo, terminal, service = await runtime(tmp_path)
    ids = iter(["India-11111111", "India-22222222", "Juliett-33333333", "India-44444444"])
    monkeypatch.setattr(agents_module, "generate_agent_id", lambda: next(ids))

    first = await register(service, "First", "Work", ["repo:first"])
    assert first["self"]["agent_id"] == "India-11111111"
    assert (await service.agent_finish("India-11111111"))["finished"] is True

    proposed = await service.agent_start(
        task_summary="Second",
        intent="Work",
        details=["Work"],
        work_scope=["repo:second"],
    )
    assert proposed["proposed_agent_id"] == "Juliett-33333333"

    old = utc_text(utc_now() - timedelta(seconds=181))
    with sqlite3.connect(repo.path) as db:
        db.execute(
            "UPDATE agent_sessions SET ended_at=? WHERE agent_id=?",
            (old, "India-11111111"),
        )
        db.commit()

    reusable = await service.agent_start(
        task_summary="Third",
        intent="Work",
        details=["Work"],
        work_scope=["repo:third"],
    )
    assert reusable["proposed_agent_id"] == "India-44444444"
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
    assert no_op["ok"] is True
    assert no_op["self"]["task_summary"] == "Plan test"
    assert no_op["self"]["details"] == [
        "Inspect current state",
        "Patch implementation",
        "Run tests",
    ]
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
async def test_agent_start_projects_full_primary_context_only(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    first = await service.context(
        "create", summary="Git workflow", content="Use the local Git wrapper.", primary=True
    )
    additional = await service.context(
        "create", summary="Docs", content="Read local docs.", primary=False
    )
    third = await service.context(
        "create", summary="Deploy", content="Use the documented deployment flow.", primary=True
    )

    started = await register(service, "Context projection", "Verify primary context")
    assert started["primary_context"] == [first["entry"], third["entry"]]
    assert all("content" in item for item in started["primary_context"])
    assert additional["entry"]["id"] not in {item["id"] for item in started["primary_context"]}

    await service.context("update", context_id=additional["entry"]["id"], primary=True)
    await service.context("delete", context_id=first["entry"]["id"])
    updated = await service.agent_start(
        agent_id=started["self"]["agent_id"], intent="Re-check primary context"
    )
    assert [item["id"] for item in updated["primary_context"]] == [
        additional["entry"]["id"],
        third["entry"]["id"],
    ]
    assert updated["primary_context"][0]["content"] == "Read local docs."

    listed = await service.context("list")
    assert all("content" not in item for item in listed["primary"] + listed["additional"])
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

    blocked = await service.run("printf 'must-not-run\n'", agent_id=receiver_id, task_scope="none")
    assert blocked["ok"] is False
    assert blocked["coordination_message_pending"] is True
    assert blocked["cmd_hash"] is None
    assert any(message_hash in line for line in blocked["pending_messages"])

    ack = await service.message(receiver_id, message_hash=message_hash)
    assert ack["ok"] is True
    assert ack["read_by"] == [receiver_name]
    assert ack.get("pending_messages", []) == []

    submitted = await service.run("printf 'after-ack\n'", agent_id=receiver_id, task_scope="none")
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
    assert sent["message_hash"] not in str(later_read.get("pending_messages", []))

    sender_status = await service.message(sender_id, message_hash=sent["message_hash"])
    assert sender_status["ok"] is True
    assert sender_status["read_by"] == []

    ack = await service.message(one_id, message_hash=sent["message_hash"])
    assert ack["read_by"] == [public_agent_name(one_id)]
    sender_status = await service.message(sender_id, message_hash=sent["message_hash"])
    assert sender_status["read_by"] == [public_agent_name(one_id)]
    two_read = await service.read("deadbeef", agent_id=two_id)
    assert any(
        sent["message_hash"] in line and "→ all:" in line for line in two_read["pending_messages"]
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

@pytest.mark.asyncio
async def test_concurrent_admission_proposals_reserve_distinct_public_names(
    tmp_path, monkeypatch
):
    repo, terminal, service = await runtime(tmp_path)
    store = AgentStore(repo.path)
    ids = iter(["Alpha-11111111", "Alpha-22222222", "Bravo-33333333"])
    monkeypatch.setattr(agents_module, "generate_agent_id", lambda: next(ids))
    original_create = store.create_proposal

    async def delayed_create(agent_id, now):
        await asyncio.sleep(0.02)
        await original_create(agent_id, now)

    service.agent_coordinator.store.create_proposal = delayed_create
    plan = {
        "task_summary": "Concurrent admission",
        "intent": "Reserve identity",
        "details": ["Reserve identity"],
        "work_scope": ["repo:synthetic"],
    }

    first, second = await asyncio.gather(
        service.agent_start(**plan),
        service.agent_start(**plan),
    )

    assert first["admission_required"] is True
    assert second["admission_required"] is True
    assert first["agent_name"] != second["agent_name"]
    assert {first["agent_name"], second["agent_name"]} == {"Alpha", "Bravo"}
    proposals = await store.recent_proposals(
        utc_text(utc_now() - timedelta(seconds=60))
    )
    assert {public_agent_name(item["agent_id"]) for item in proposals} >= {
        "Alpha",
        "Bravo",
    }
    await terminal.stop()


@pytest.mark.asyncio
async def test_two_step_admission_does_not_persist_until_confirmed(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    store = AgentStore(repo.path)

    proposal = await service.agent_start(
        task_summary="Synthetic task",
        intent="Inspect synthetic fixture",
        details=["Inspect synthetic fixture", "Run deterministic tests"],
        work_scope=["repo:synthetic"],
    )

    assert proposal["ok"] is False
    assert proposal["admission_required"] is True
    assert proposal["proposed_agent_id"].startswith(proposal["agent_name"] + "-")
    assert len(proposal["proposed_agent_id"].rsplit("-", 1)[1]) == 8
    assert await store.get_session(proposal["proposed_agent_id"]) is None

    confirmed = await service.agent_start(
        agent_id=proposal["proposed_agent_id"],
        task_summary="Synthetic task",
        intent="Inspect synthetic fixture",
        details=["Inspect synthetic fixture", "Run deterministic tests"],
        work_scope=["repo:synthetic"],
    )
    assert confirmed["ok"] is True
    assert confirmed["self"]["agent_id"] == proposal["proposed_agent_id"]
    assert (await store.get_session(proposal["proposed_agent_id"]))["state"] == "active"
    await terminal.stop()


@pytest.mark.asyncio
async def test_unknown_full_identity_fails_closed_without_trusted_fleet_record(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    agent_id = "Victor-01234567"

    rejected = await service.agent_start(
        agent_id=agent_id,
        task_summary="Synthetic resume",
        intent="Reuse cross-server identity",
        details=["Reuse cross-server identity"],
    )

    assert rejected["ok"] is False
    assert rejected["foreign_identity_unavailable"] is True
    assert rejected["retryable"] is True
    assert await AgentStore(repo.path).get_session(agent_id) is None
    await terminal.stop()


@pytest.mark.asyncio
async def test_ended_identity_returns_to_chat_and_cannot_be_recreated(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    started = await register(service, "Synthetic finish", "Finish safely")
    agent_id = started["self"]["agent_id"]

    assert (await service.agent_finish(agent_id))["finished"] is True
    ended = await service.agent_start(
        agent_id=agent_id,
        task_summary="Must not restart",
        intent="Must return to chat",
        details=["Must return to chat"],
    )

    assert ended["ok"] is False
    assert ended["return_to_chat"] is True
    assert ended["session_status"] == "finished"
    assert (await AgentStore(repo.path).get_session(agent_id))["state"] == "finished"
    await terminal.stop()


@pytest.mark.asyncio
async def test_unknown_identity_requires_40_bit_suffix(tmp_path):
    repo, terminal, service = await runtime(tmp_path)

    rejected = await service.agent_start(
        agent_id="Alpha-1234",
        task_summary="Legacy external id",
        intent="Should not admit",
        details=["Should not admit"],
    )
    assert rejected["ok"] is False
    assert rejected["admission_required"] is True
    assert "40-bit" in rejected["error"]
    await terminal.stop()
