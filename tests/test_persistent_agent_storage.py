import sqlite3

import pytest

from terminal_mcp.core.orchestration import live_task_claims
from terminal_mcp.core.persistent_agents import ArmGeneration, ClaimOwner, WorkSessionRecord
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.persistent_agents import PersistentAgentStore
from terminal_mcp.storage.sqlite import SCHEMA_VERSION, SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


async def stores(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    return repo, PersistentAgentStore(repo.path), TaskStore(repo.path)


@pytest.mark.asyncio
async def test_v15_persistent_slot_schema_and_repository_round_trip(tmp_path):
    repo, persistent, _ = await stores(tmp_path)
    with sqlite3.connect(repo.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 18
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "logical_agents",
        "logical_agent_selectors",
        "logical_agent_arms",
        "logical_agent_work_sessions",
    } <= tables

    slot = await persistent.create_slot(
        "logical-1", "Alpha", "A123", authority_node_id="node-home", now="2026-01-01T00:00:00Z"
    )
    assert slot.logical_agent_id == "logical-1"
    assert slot.state == "suspended"
    assert (await persistent.selector_owner("a123"))["logical_agent_id"] == "logical-1"

    generation = await persistent.rotate_selector(
        "logical-1", "B123", expected_generation=1, now="2026-01-01T00:01:00Z"
    )
    assert generation == 2
    retired = await persistent.selector_owner("A123")
    assert retired["retired_at"] == "2026-01-01T00:01:00Z"
    with pytest.raises(sqlite3.IntegrityError):
        await persistent.create_slot("logical-2", "Bravo", "A123", authority_node_id="node-home")

    arm = ArmGeneration(
        "logical-1",
        1,
        "2026-01-01T00:02:00Z",
        "2026-01-01T00:25:00Z",
        1380,
        2,
        1,
        2,
    )
    await persistent.record_arm(arm)
    assert await persistent.get_arm("logical-1", 1) == arm

    session = WorkSessionRecord(
        "session-1",
        "logical-1",
        1,
        "node-home",
        1,
        "2026-01-01T00:02:00Z",
        "2026-01-01T00:25:00Z",
        "user-1",
        1,
        "active",
        origin_instance_id="node-home",
    )
    await persistent.record_work_session(session)
    assert await persistent.get_work_session("session-1") == session
    with pytest.raises(sqlite3.IntegrityError):
        await persistent.record_work_session(
            WorkSessionRecord(
                "session-2",
                "logical-1",
                1,
                "node-home",
                1,
                "2026-01-01T00:03:00Z",
                "2026-01-01T00:26:00Z",
                "user-1",
                1,
                "active",
            )
        )
    assert await persistent.persistent_state_exists() is True


@pytest.mark.asyncio
async def test_persistent_claim_owner_is_not_legacy_session_liveness(tmp_path):
    repo, persistent, tasks = await stores(tmp_path)
    await persistent.create_slot("logical-1", "Alpha", "A123", authority_node_id="node-home")
    await tasks.create_task("ns", "T-1", "Task", cooperative=True)
    await tasks.claim("ns", "T-1", "Alpha-1111", claim_intent="legacy")
    await tasks.claim_owner(
        "ns", "T-1", ClaimOwner.logical_agent("logical-1"), claim_intent="persistent"
    )

    class NoSessions:
        async def get_session(self, agent_id):
            return None

    live = await live_task_claims(
        tasks,
        NoSessions(),
        "ns",
        "T-1",
        idle_ttl_seconds=60,
        max_session_seconds=600,
    )
    assert [(item["owner_kind"], item["owner_id"]) for item in live] == [
        ("logical_agent", "logical-1")
    ]

    # A legacy cleanup for an identically-named session cannot release a Persistent owner.
    assert await tasks.release_claims(agent_id="logical-1") == 0
    claims = await tasks.active_claims("ns", "T-1")
    assert any(item["owner_kind"] == "logical_agent" for item in claims)
    assert (
        await tasks.release_owner_claim("ns", "T-1", ClaimOwner.logical_agent("logical-1")) is True
    )


@pytest.mark.asyncio
async def test_session_provenance_round_trips_without_changing_legacy_shapes(tmp_path):
    repo, _, tasks = await stores(tmp_path)
    agents = AgentStore(repo.path)
    command = await repo.create(
        "echo ok",
        status="queued",
        agent_id="Alpha-1111",
        logical_agent_id="logical-1",
        work_session_id="session-7",
        session_epoch=7,
    )
    detail = await agents.command_detail(command.cmd_hash)
    assert detail["logical_agent_id"] == "logical-1"
    assert detail["work_session_id"] == "session-7"
    assert detail["session_epoch"] == 7

    await tasks.create_task("ns", "T-1", "Task")
    event = await tasks.add_event(
        "ns",
        "T-1",
        "command",
        agent_id="Alpha-1111",
        logical_agent_id="logical-1",
        work_session_id="session-7",
        session_epoch=7,
    )
    assert event["session_epoch"] == 7
    listed = await tasks.list_events("ns", "T-1")
    assert listed[0]["logical_agent_id"] == "logical-1"
    assert listed[0]["work_session_id"] == "session-7"


@pytest.mark.asyncio
async def test_schema_forward_guard_refuses_newer_database(tmp_path):
    database = tmp_path / "future.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA user_version=19")
    repo = SqliteRepository(database, tmp_path / "output.sqlite3")
    with pytest.raises(RuntimeError, match="newer than supported"):
        await repo.initialize()
