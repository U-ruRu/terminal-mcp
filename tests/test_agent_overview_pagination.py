"""Isolated SQLite/read fixtures for bounded overview pagination completeness."""

import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.core import read_contract
from terminal_mcp.core.human_names import number_agent_name
from terminal_mcp.mcp.access_contracts import MeshAgentObserve
from terminal_mcp.storage.access_mesh import AccessMeshStore

T0 = datetime(2026, 1, 1, tzinfo=UTC)
ACTOR = SimpleNamespace(endpoint_role="coordinator")


@pytest.fixture
def overview_fixture(tmp_path, monkeypatch):
    """Only tables queried by the projection; no transport or lifecycle actions."""
    path = tmp_path / "overview.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE access_mesh_slot_replicas(
                issuer_id TEXT,slot_id TEXT,state TEXT);
            CREATE TABLE access_mesh_sessions(issuer_id TEXT,slot_id TEXT,logical_agent_id TEXT);
            CREATE TABLE access_mesh_attachments(
                connection_key TEXT,issuer_id TEXT,slot_id TEXT);
            CREATE TABLE access_mesh_local_sessions(
                issuer_id TEXT,slot_id TEXT,work_session_id TEXT,session_epoch INTEGER,
                current_intent TEXT,intent_updated_at TEXT);
            CREATE TABLE logical_agent_work_sessions(
                work_session_id TEXT,state TEXT,started_at TEXT,hard_expires_at TEXT);
            CREATE TABLE logical_agents(logical_agent_id TEXT,display_name TEXT);
            CREATE TABLE access_mesh_activity(connection_key TEXT,last_active_at TEXT);
            CREATE TABLE access_mesh_number_claims(
                issuer_id TEXT,slot_id TEXT,number TEXT,
                started_at TEXT,hard_expires_at TEXT);
        """)
    skipped = set()
    queries = []

    def connect():
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        db.set_trace_callback(queries.append)
        return db

    store = object.__new__(AccessMeshStore)
    store.local_node_id = "server-a"
    monkeypatch.setattr(store, "_connect", connect)
    monkeypatch.setattr(
        store,
        "_snapshot",
        lambda row, **kwargs: SimpleNamespace(
            session=SimpleNamespace(logical_agent_id=row["session_agent_id"]),
            issuer_id=row["issuer_id"],
            slot_id=row["slot_id"],
            agent_name=number_agent_name(f"{int(row['session_agent_id'].rsplit('_', 1)[1]):04d}"),
            display_suffix=row["display_name"],
        ),
    )
    monkeypatch.setattr(
        store,
        "merged_cycle",
        lambda slot, now: {
            "state": "expired" if slot.session.logical_agent_id in skipped else "active",
            "started_at": T0.isoformat(),
            "hard_expires_at": (T0 + timedelta(hours=1)).isoformat(),
        },
    )
    app = object.__new__(AccessMeshApplication)
    app.store = store
    app.clock = lambda: T0

    def populate(count, *, intent_size=20):
        with sqlite3.connect(path) as db:
            for index in range(count):
                agent = f"la_fixture_{index:03d}"
                slot, session, connection = f"slot-{index}", f"session-{index}", f"conn-{index}"
                db.execute(
                    "INSERT INTO access_mesh_slot_replicas VALUES(?,?,'active')",
                    ("server-a", slot),
                )
                db.execute(
                    "INSERT INTO access_mesh_sessions VALUES(?,?,?)", ("server-a", slot, agent),
                )
                db.execute(
                    "INSERT INTO access_mesh_attachments VALUES(?,?,?)",
                    (connection, "server-a", slot),
                )
                db.execute(
                    "INSERT INTO access_mesh_local_sessions VALUES(?,?,?,?,?,?)",
                    ("server-a", slot, session, 1, "x" * intent_size, T0.isoformat()),
                )
                db.execute(
                    "INSERT INTO logical_agent_work_sessions VALUES(?,'active',?,?)",
                    (session, T0.isoformat(), (T0 + timedelta(hours=1)).isoformat()),
                )
                db.execute(
                    "INSERT INTO logical_agents VALUES(?,?)", (agent, f"Fixture agent {index}")
                )
                db.execute(
                    "INSERT INTO access_mesh_activity VALUES(?,?)", (connection, T0.isoformat())
                )
        return [f"la_fixture_{index:03d}" for index in range(count)]

    return SimpleNamespace(
        app=app, store=store, skipped=skipped, populate=populate, queries=queries
    )


async def collect(app, *, limit, include_inactive=False):
    records, pages, cursor = [], [], None
    while True:
        result = await app.observe(
            ACTOR, limit=limit, cursor=cursor, include_inactive=include_inactive
        )
        MeshAgentObserve.model_validate(result)
        pages.append(result)
        records.extend(result["agents"])
        cursor = result.get("next_cursor")
        if cursor is None:
            return records, pages


async def test_filtered_page_cursor_reaches_later_live_agent(overview_fixture):
    f = overview_fixture
    agents = f.populate(3)
    f.skipped.update(agents[:2])
    first = await f.app.observe(ACTOR, limit=1)
    second = await f.app.observe(ACTOR, limit=1, cursor=first["next_cursor"])
    assert [record["logical_agent_id"] for record in second["agents"]] == agents[2:]


async def test_byte_budget_cursor_retains_unemitted_records(overview_fixture, monkeypatch):
    f = overview_fixture
    agents = f.populate(3, intent_size=1000)
    first_record = f.store.agent_overview(now=T0, limit=3).agents[0]
    budget = read_contract.mirrored_json_size(first_record) * 2 + 10
    original = read_contract.bounded_page

    def smaller_page(items, **kwargs):
        return original(items, budget_bytes=budget, **kwargs)

    monkeypatch.setattr(read_contract, "bounded_page", smaller_page)
    first = await f.app.observe(ACTOR, limit=3)
    second = await f.app.observe(ACTOR, limit=3, cursor=first["next_cursor"])
    assert [record["logical_agent_id"] for record in second["agents"]] == agents[2:]


async def test_cursor_remains_bound_to_filters(overview_fixture):
    f = overview_fixture
    agents = f.populate(3)
    f.skipped.update(agents[:2])
    result = await f.app.observe(ACTOR, limit=1)
    with pytest.raises(read_contract.InvalidCursor):
        await f.app.observe(ACTOR, limit=1, cursor=result["next_cursor"], include_inactive=True)


def claim(fixture, index, start_minutes, end_minutes, *, number="1000"):
    with fixture.store._connect() as db:
        db.execute(
            "INSERT INTO access_mesh_number_claims VALUES(?,?,?,?,?)",
            (
                "server-a",
                f"slot-{index}",
                number,
                (T0 + timedelta(minutes=start_minutes)).isoformat(),
                (T0 + timedelta(minutes=end_minutes)).isoformat(),
            ),
        )


async def test_losing_attachment_projects_current_winner_session(overview_fixture):
    f = overview_fixture
    agents = f.populate(2)
    claim(f, 0, 0, 20)
    claim(f, 1, 1, 20)
    latest = (T0 + timedelta(seconds=12)).isoformat()
    with f.store._connect() as db:
        db.execute("DELETE FROM access_mesh_attachments WHERE slot_id='slot-1'")
        db.execute(
            "UPDATE access_mesh_local_sessions SET current_intent='canonical intent' "
            "WHERE slot_id='slot-1'"
        )
        db.execute(
            "UPDATE access_mesh_activity SET last_active_at=? WHERE connection_key='conn-0'",
            (latest,),
        )
    result = await f.app.observe(ACTOR, logical_agent_id=agents[1])
    MeshAgentObserve.model_validate(result)
    assert len(result["agents"]) == 1
    record = result["agents"][0]
    assert record["logical_agent_id"] == agents[1]
    assert record["display_name"] == "Bravo-1 - Fixture agent 1"
    assert record["work_session_id"] == "session-1"
    assert record["current_intent"] == "canonical intent"
    assert record["last_activity"] == latest


async def test_aliases_collapse_before_page_limit(overview_fixture):
    f = overview_fixture
    agents = f.populate(4)
    for index in range(3):
        claim(f, index, index, 20)
    latest = (T0 + timedelta(seconds=15)).isoformat()
    with f.store._connect() as db:
        db.execute(
            "UPDATE access_mesh_activity SET last_active_at=? WHERE connection_key='conn-0'",
            (latest,),
        )
        db.execute(
            "UPDATE access_mesh_local_sessions SET current_intent=NULL,intent_updated_at=NULL "
            "WHERE slot_id='slot-2'"
        )
    page = await f.app.observe(ACTOR, limit=1)
    assert len(page["agents"]) == 1
    record = page["agents"][0]
    assert record["logical_agent_id"] == agents[2]
    assert record["last_activity"] == latest
    assert page["next_cursor"]
