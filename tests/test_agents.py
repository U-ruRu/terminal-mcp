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
    await store.create_session("Alpha-1111", "one", "first", ["repo:storage"], now)
    with pytest.raises(sqlite3.IntegrityError):
        await store.create_session("Alpha-1111", "two", "second", ["repo:mcp"], now)

    ids = iter(["Alpha-9999", "Bravo-2222"])
    monkeypatch.setattr(agents_module, "generate_agent_id", lambda: next(ids))
    created = await service.agent_start("Implement registry", "Inspect schema", ["repo:storage"])
    assert created["self"]["agent_id"] == "Bravo-2222"
    updated = await service.agent_task("Bravo-2222", "Edit schema")
    assert updated["self"]["name"] == "Bravo"
    assert "agent_id" not in updated["self"]
    assert updated["self"]["intent"] == "Edit schema"
    assert updated["self"]["work_scope"] == ["repo:storage"]
    with sqlite3.connect(repo.path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM agent_task_events WHERE agent_id='Bravo-2222'"
        ).fetchone()[0]
    assert count == 2
    await terminal.stop()


@pytest.mark.asyncio
async def test_activity_refresh_expiry_and_reregistration(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    started = await service.agent_start("TTL test", "Observe", ["repo:tests"])
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
    fresh = await service.agent_start("TTL test", "Continue", ["repo:tests"])
    assert fresh["self"]["agent_id"] != agent_id
    await terminal.stop()


@pytest.mark.asyncio
async def test_command_attribution_recent_order_and_global_read(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    started = await service.agent_start("Attribution", "Run commands", ["repo:tests"])
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
    a = await service.agent_start(
        "Implement storage changes", "Edit SQLite schema", ["repo:storage"]
    )
    aid = a["self"]["agent_id"]
    command = await service.run("printf 'agent-a\\n'", agent_id=aid)
    await wait_finished(service, aid, command["cmd_hash"])

    b = await service.agent_start("Review MCP", "Inspect active agent", ["repo:mcp"])
    bid = b["self"]["agent_id"]
    seen = next(item for item in b["active"] if item["name"] == public_agent_name(aid))
    assert seen["intent"] == "Edit SQLite schema"
    assert seen["recent_commands"][0]["command_hash"] == command["cmd_hash"]
    overlap = await service.agent_task(bid, "Edit storage adapter")
    assert overlap["overlaps"] == []
    assert overlap["self"]["work_scope"] == ["repo:mcp"]

    global_read = await service.read(None, 20, 0)
    assert any(
        f"{public_agent_name(aid)} {command['cmd_hash']}" in line
        for line in global_read["lines"]
    )
    assert len(overlap["active"]) <= 8
    assert all(len(item["recent_commands"]) <= 3 for item in overlap["active"])

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
    started = await service.agent_start("Finish", "Finish session", ["repo:tests"])
    agent_id = started["self"]["agent_id"]
    old = utc_text(utc_now() - timedelta(seconds=600))
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE agent_sessions SET last_activity_at=? WHERE agent_id=?", (old, agent_id))
        db.commit()
    service.agent_coordinator.ttl_seconds = 300
    expired = await service.agents(agent_id)
    assert expired["registration_required"] is True

    fresh = await service.agent_start("Finish", "Finish session", ["repo:tests"])
    fid = fresh["self"]["agent_id"]
    finished = await service.agent_finish(fid)
    assert finished == {
        "ok": True,
        "agent_name": public_agent_name(fid),
        "finished": True,
    }
    assert (await service.agents(fid))["registration_required"] is True
    await terminal.stop()


@pytest.mark.asyncio
async def test_active_overview_enforces_eight_agent_limit(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    caller = await service.agent_start("Caller", "Observe peers", ["repo:tests"])
    caller_id = caller["self"]["agent_id"]
    for index in range(10):
        await service.agent_start(f"Peer {index}", f"Task {index}", [f"repo:peer/{index}"])
    overview = await service.agents(caller_id)
    assert len(overview["active"]) == 8
    assert overview["additional_active_agents"] == 2
    await terminal.stop()

@pytest.mark.asyncio
async def test_run_requires_fresh_task_lease_and_agent_task_refreshes_it(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    started = await service.agent_start("Lease test", "Initial task", ["repo:tests"])
    agent_id = started["self"]["agent_id"]
    assert started["self"]["task_lease_seconds"] == 180
    service.agent_coordinator.task_lease_seconds = 0.01
    await asyncio.sleep(0.03)
    blocked = await service.run("printf stale", agent_id=agent_id)
    assert blocked["ok"] is False
    assert blocked["task_context_expired"] is True
    assert blocked["max_task_age_seconds"] == 0.01
    assert "agent_task" in blocked["error"]
    refreshed = await service.agent_task(agent_id, "Refreshed task")
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
    first = await service.agent_start("First", "Observe peer", ["repo:first"])
    first_id = first["self"]["agent_id"]
    second = await service.agent_start("Second", "Run peer command", ["repo:second"])
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
    assert all(re.match(r"^\d{2}:\d{2}:\d{2} ", line) for line in peers)
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
    started = await service.agent_start("TTL refresh", "Initial intent", ["repo:tests"])
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
    task = await service.agent_task(agent_id, "New short intent")
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

    first = await service.agent_start("First", "Work", ["repo:first"])
    assert first["self"]["agent_id"] == "India-1111"
    assert (await service.agent_finish("India-1111"))["finished"] is True

    second = await service.agent_start("Second", "Work", ["repo:second"])
    assert second["self"]["agent_id"] == "Juliett-3333"
    assert second["self"]["name"] == "Juliett"
    await terminal.stop()
