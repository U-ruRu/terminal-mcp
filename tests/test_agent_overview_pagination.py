"""Isolated SQLite/read fixtures for bounded overview pagination completeness."""

import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.core import read_contract
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
                issuer_id TEXT,slot_id TEXT,logical_agent_id TEXT,state TEXT);
            CREATE TABLE access_mesh_attachments(
                connection_key TEXT,issuer_id TEXT,slot_id TEXT);
            CREATE TABLE access_mesh_local_sessions(
                issuer_id TEXT,slot_id TEXT,work_session_id TEXT,session_epoch INTEGER,
                current_intent TEXT,intent_updated_at TEXT);
            CREATE TABLE logical_agent_work_sessions(
                work_session_id TEXT,state TEXT,started_at TEXT,hard_expires_at TEXT);
            CREATE TABLE logical_agents(logical_agent_id TEXT,display_name TEXT);
            CREATE TABLE access_mesh_activity(connection_key TEXT,last_active_at TEXT);
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
        lambda row: SimpleNamespace(
            logical_agent_id=row["logical_agent_id"],
            issuer_id=row["issuer_id"],
            slot_id=row["slot_id"],
        ),
    )
    monkeypatch.setattr(
        store,
        "merged_cycle",
        lambda slot, now: {
            "state": "expired" if slot.logical_agent_id in skipped else "active",
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
                    "INSERT INTO access_mesh_slot_replicas VALUES(?,?,?,'active')",
                    ("server-a", slot, agent),
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


async def test_empty_filtered_window_keeps_cursor_to_later_live_agent(overview_fixture):
    f = overview_fixture
    agents = f.populate(3)
    f.skipped.update(agents[:2])
    records, pages = await collect(f.app, limit=1)
    assert pages[0]["agents"] == [] and pages[0]["next_cursor"]
    assert [record["logical_agent_id"] for record in records] == agents[2:]
    assert "next_cursor" not in pages[-1]


async def test_mixed_filtered_rows_do_not_skip_or_repeat_visible_agents(overview_fixture):
    f = overview_fixture
    agents = f.populate(6)
    f.skipped.update((agents[1], agents[3]))
    records, pages = await collect(f.app, limit=2)
    assert [record["logical_agent_id"] for record in records] == [agents[i] for i in (0, 2, 4, 5)]
    assert all(len(page["agents"]) <= 2 for page in pages)


async def test_byte_budget_retains_unemitted_records(overview_fixture, monkeypatch):
    f = overview_fixture
    agents = f.populate(5, intent_size=1000)
    first = f.store.agent_overview(now=T0, limit=5).agents[0]
    budget = read_contract.mirrored_json_size(first) * 2 + 10
    original = read_contract.bounded_page

    def smaller_page(items, **kwargs):
        return original(items, budget_bytes=budget, **kwargs)

    monkeypatch.setattr(read_contract, "bounded_page", smaller_page)
    records, pages = await collect(f.app, limit=4)
    assert len(pages[0]["agents"]) == 2 and pages[0]["next_cursor"]
    assert [record["logical_agent_id"] for record in records] == agents


async def test_include_inactive_keeps_records_and_private_positions_out_of_wire(overview_fixture):
    f = overview_fixture
    agents = f.populate(4)
    f.skipped.update(agents[:3])
    records, pages = await collect(f.app, limit=1, include_inactive=True)
    assert [record["logical_agent_id"] for record in records] == agents
    assert [record["session_state"] for record in records] == ["expired"] * 3 + ["active"]
    for page in pages:
        assert set(page) <= {"ok", "scope", "server_id", "agents", "next_cursor"}
        assert all(
            "row_offsets" not in record and "scanned_until" not in record
            for record in page["agents"]
        )


async def test_raw_scan_bound_and_empty_pages_eventually_terminate(overview_fixture):
    f = overview_fixture
    agents = f.populate(205)
    f.skipped.update(agents[:203])
    scan = f.store.agent_overview(now=T0, limit=101)
    assert scan.agents == [] and scan.scanned_until == 100 and scan.has_more
    assert any("LIMIT 101 OFFSET 0" in query for query in f.queries)
    records, pages = await collect(f.app, limit=100)
    assert len(pages) == 3
    assert pages[0]["agents"] == pages[1]["agents"] == []
    assert [record["logical_agent_id"] for record in records] == agents[203:]


async def test_exhausted_fully_filtered_window_has_no_false_continuation(overview_fixture):
    f = overview_fixture
    agents = f.populate(4)
    f.skipped.update(agents)
    records, pages = await collect(f.app, limit=1)
    assert records == [] and len(pages) == 2
    assert pages[0]["next_cursor"] and "next_cursor" not in pages[-1]


async def test_cursor_remains_bound_to_filters(overview_fixture):
    f = overview_fixture
    agents = f.populate(3)
    f.skipped.update(agents[:2])
    result = await f.app.observe(ACTOR, limit=1)
    with pytest.raises(read_contract.InvalidCursor):
        await f.app.observe(ACTOR, limit=1, cursor=result["next_cursor"], include_inactive=True)
