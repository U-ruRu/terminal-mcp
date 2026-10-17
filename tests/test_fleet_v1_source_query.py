"""Current Fleet query plane over its own SQLite data, with minimal page boundaries."""

import sqlite3

import pytest

from terminal_mcp.fleet.source_query import FleetSourceQueryPlane
from terminal_mcp.storage.sqlite import SqliteRepository


@pytest.fixture
async def query_plane(tmp_path):
    repo = SqliteRepository(tmp_path / "runtime.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    with sqlite3.connect(repo.path) as db:
        db.executemany(
            "INSERT INTO instance_context(summary,content,is_primary) VALUES(?,?,?)",
            [("first", "first-content", 0), ("second", "second-content", 1)],
        )
    return FleetSourceQueryPlane(repo.path)


@pytest.mark.asyncio
async def test_query_cursor_starts_after_the_preceding_row(query_plane):
    previous = await query_plane.query("contexts", limit=1)

    page = await query_plane.query("contexts", cursor=previous["next_cursor"], limit=1)

    assert [item["summary"] for item in page["items"]] == ["second"]


@pytest.mark.asyncio
async def test_current_recovery_returns_a_bounded_page(query_plane):
    page = await query_plane.recovery_page(
        scope="contexts", generation="gen-a", barrier=3, high_water=3, limit=1
    )

    assert page["page_rows"] == 1
    assert page["page_complete"] is False
    assert page["next_cursor"] is not None


@pytest.mark.asyncio
async def test_query_search_filters_current_rows(query_plane):
    page = await query_plane.query("contexts", q="second", include_count=True)

    assert [item["summary"] for item in page["items"]] == ["second"]
    assert page["count"] == 1


@pytest.mark.asyncio
async def test_detail_returns_the_requested_context(query_plane):
    row = await query_plane.detail("contexts", 2)

    assert row["summary"] == "second"
    assert row["content"] == "second-content"
