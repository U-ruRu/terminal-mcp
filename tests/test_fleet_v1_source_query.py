import aiosqlite
import pytest

from terminal_mcp.fleet.source import FleetSourceService
from terminal_mcp.fleet.source_meta import FleetNodeMetaStore
from terminal_mcp.storage.events import EventJournalStore
from terminal_mcp.storage.sqlite import SqliteRepository


@pytest.mark.asyncio
async def test_current_recovery_is_bounded_and_history_remains_queryable(tmp_path):
    runtime = tmp_path / "runtime.sqlite3"
    output = tmp_path / "output.sqlite3"
    repo = SqliteRepository(runtime, output)
    await repo.initialize()
    async with aiosqlite.connect(runtime) as db:
        rows = []
        for index in range(130):
            state = "ready" if index < 105 else "done"
            rows.append(
                (
                    "scope",
                    f"T-{index:03d}",
                    f"Task {index}",
                    "implementation",
                    index,
                    state,
                    "2026-09-30T00:00:00Z",
                    "2026-09-30T00:00:00Z" if state == "ready" else None,
                    "2026-09-30T00:00:00Z",
                    "2026-09-30T00:00:00Z",
                )
            )
        await db.executemany(
            "INSERT INTO work_items(namespace,task_id,title,lane,priority,state,"
            "state_changed_at,ready_since,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        await db.commit()

    journal = EventJournalStore(runtime)
    meta = FleetNodeMetaStore(
        tmp_path / "fleet-node-meta.sqlite3", fleet_id="fleet-a", node_id="node-a"
    )
    await meta.initialize()
    source = FleetSourceService(runtime, journal, meta, output_db_path=output)

    boot = await source.bootstrap()
    assert boot["current_scopes"]
    first = await source.current_recovery("tasks", limit=100)
    assert first["page_rows"] == 100
    assert first["page_bytes"] <= 512 * 1024
    assert first["page_complete"] is False
    assert first["replace_scope"] is True
    assert all(item["payload"]["state"] == "ready" for item in first["entities"])

    second = await source.current_recovery(
        "tasks",
        snapshot_id=first["snapshot_id"],
        cursor=first["next_cursor"],
        limit=100,
    )
    assert second["page_rows"] == 5
    assert second["page_complete"] is True
    assert second["barrier_source_seq"] == first["barrier_source_seq"]

    history = await source.query(
        "tasks", filters={"state": "done"}, include_count=True, include_facets=True
    )
    assert history["count"] == 25
    assert len(history["items"]) == 25
    detail = await source.detail("tasks", "scope/T-129")
    assert detail["state"] == "done"

    namespaces = await source.query_namespaces()
    assert namespaces["namespaces"] == ["scope"]


@pytest.mark.asyncio
async def test_query_keyset_reaches_beyond_first_page(tmp_path):
    runtime = tmp_path / "runtime.sqlite3"
    output = tmp_path / "output.sqlite3"
    repo = SqliteRepository(runtime, output)
    await repo.initialize()
    async with aiosqlite.connect(runtime) as db:
        await db.executemany(
            "INSERT INTO instance_context(summary,content,is_primary) VALUES(?,?,?)",
            [(f"context-{i:03d}", "x", 0) for i in range(115)],
        )
        await db.commit()
    journal = EventJournalStore(runtime)
    meta = FleetNodeMetaStore(
        tmp_path / "fleet-node-meta.sqlite3", fleet_id="fleet-a", node_id="node-a"
    )
    await meta.initialize()
    source = FleetSourceService(runtime, journal, meta, output_db_path=output)

    first = await source.query("contexts", limit=100)
    second = await source.query("contexts", cursor=first["next_cursor"], limit=100)
    assert len(first["items"]) == 100
    assert len(second["items"]) == 15
    assert second["complete"] is True
