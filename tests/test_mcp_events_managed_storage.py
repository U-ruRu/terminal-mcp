"""Managed event-store projection owns its current temporary SQLite database."""

import json
import sqlite3
from datetime import timedelta

import pytest

from terminal_mcp.core.orchestration import parse_utc, utc_now, utc_text
from terminal_mcp.mcp.event_store import MCPEventStore
from terminal_mcp.storage.sqlite import SqliteRepository

NODE = "secondary-qa"


@pytest.fixture
async def managed(tmp_path):
    db_path = tmp_path / "state.sqlite3"
    repo = SqliteRepository(db_path)
    await repo.initialize()
    events = MCPEventStore(db_path, NODE)
    await events.initialize()
    return db_path, events


def seed_managed(path, *, authority=NODE, origin=NODE, age=0, state="active"):
    stamp = utc_text(utc_now() - timedelta(seconds=age))
    expiry = utc_text(utc_now() + timedelta(seconds=1380))
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO logical_agents("
            "logical_agent_id,display_name,state,authority_node_id,authority_epoch,"
            "created_at,updated_at) VALUES('la_live','Visible Agent','active',?,1,?,?)",
            (authority, stamp, stamp),
        )
        db.execute(
            "INSERT INTO logical_agent_work_sessions("
            "work_session_id,logical_agent_id,session_epoch,authority_node_id,"
            "authority_epoch,started_at,hard_expires_at,auth_generation,state,"
            "origin_instance_id) VALUES('ws_live','la_live',1,?,1,?,?,1,?,?)",
            (authority, stamp, expiry, state, origin),
        )
    return stamp


def rows(path, event_name=None):
    with sqlite3.connect(path) as db:
        result = db.execute(
            "SELECT name,source_key,data_json,event_id FROM mcp_event_records ORDER BY rowid"
        ).fetchall()
    data = [
        {"name": name, "source_key": source, "data": json.loads(payload), "id": event_id}
        for name, source, payload, event_id in result
    ]
    return [row for row in data if row["name"] == event_name] if event_name else data


@pytest.mark.asyncio
async def test_explicit_local_attach_callback_creates_one_event_without_neighbor_presence_rows(
    managed,
):
    path, events = managed
    seed_managed(path)
    # Bare WorkSession creation is NOT an attach. Only the successful verified
    # admission callback on this server may record local presence.
    await events.pump()
    assert rows(path, "agent.attached") == []
    assert await events.note_local_attach("la_live", "ws_live", 1, agent_name="Visible Agent")
    with sqlite3.connect(path) as db:
        for table in (
            "agent_sessions",
            "persistent_attachment_presence",
            "logical_agent_node_attachments",
        ):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    await events.pump()
    attached = rows(path, "agent.attached")
    assert len(attached) == 1
    assert attached[0]["data"] == {"agent_name": "Visible Agent", "server": NODE}
    await events.pump()
    assert rows(path, "agent.attached") == attached


@pytest.mark.asyncio
async def test_foreign_work_session_and_grant_not_a_local_attach(managed):
    path, events = managed
    seed_managed(path, authority="remote", origin="remote")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO logical_agent_node_attachments("
            "node_attachment_id,logical_agent_id,work_session_id,session_epoch,"
            "node_instance_id,authority_epoch,attached_at,hard_expires_at)"
            "VALUES('only_grant','la_live','ws_live',1,?,1,?,?)",
            (
                "remote",
                utc_text(),
                utc_text(utc_now() + timedelta(minutes=10)),
            ),
        )
    await events.pump()
    assert rows(path, "agent.attached") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["ended", "expired"])
async def test_managed_end_without_presence_once_even_after_metadata_retry(managed, terminal):
    path, events = managed
    seed_managed(path)
    await events.note_local_attach("la_live", "ws_live", 1)
    await events.pump()
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE logical_agent_work_sessions SET state=?,ended_at=?,end_reason=?"
            " WHERE work_session_id='ws_live'",
            (terminal, utc_text(), "session_end" if terminal == "ended" else "hard_duration"),
        )
    await events.pump()
    first = rows(path, "session.ended")
    assert len(first) == 1
    assert first[0]["data"] == {
        "agent_name": "Visible Agent",
        "server": NODE,
        "reason": "manual" if terminal == "ended" else "expired",
    }
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE logical_agent_work_sessions SET end_reason='retry_cleanup' "
            "WHERE work_session_id='ws_live'"
        )
    await events.pump()
    assert rows(path, "session.ended") == first


@pytest.mark.asyncio
async def test_managed_inactivity_strict_180_once_per_real_activity_episode(managed):
    path, events = managed
    started = seed_managed(path, age=1)
    start = parse_utc(started)
    await events.note_local_attach("la_live", "ws_live", 1, now=start)
    await events.pump(now=start + timedelta(seconds=180))
    assert rows(path, "agent.inactive") == []
    await events.pump(now=start + timedelta(seconds=180, microseconds=1))
    first = rows(path, "agent.inactive")
    assert len(first) == 1
    await events.pump(now=start + timedelta(seconds=300))
    assert rows(path, "agent.inactive") == first
    new_activity = start + timedelta(seconds=301)
    assert await events.note_managed_activity("la_live", "ws_live", 1, now=new_activity)
    await events.pump(now=new_activity + timedelta(seconds=180))
    assert len(rows(path, "agent.inactive")) == 1
    await events.pump(now=new_activity + timedelta(seconds=181))
    assert len(rows(path, "agent.inactive")) == 2
    await events.pump(now=new_activity + timedelta(seconds=230))
    assert len(rows(path, "agent.inactive")) == 2


@pytest.mark.asyncio
async def test_observer_is_read_only_for_managed_idle_clock(managed):
    path, events = managed
    started = seed_managed(path, age=200)
    await events.note_local_attach("la_live", "ws_live", 1, now=parse_utc(started))
    with sqlite3.connect(path) as db:
        db.execute("SELECT * FROM logical_agent_work_sessions").fetchall()
        db.execute("SELECT * FROM logical_agents").fetchall()
        assert db.execute("SELECT COUNT(*) FROM mcp_event_managed_activity").fetchone()[0] == 0
    await events.pump()
    assert len(rows(path, "agent.inactive")) == 1
    await events.pump()
    assert len(rows(path, "agent.inactive")) == 1


@pytest.mark.asyncio
async def test_note_activity_rejects_unrelated_session_or_expired_session(managed):
    path, events = managed
    seed_managed(path)
    assert await events.note_local_attach("la_live", "ws_live", 1)
    assert not await events.note_managed_activity("other", "ws_live", 1)
    assert not await events.note_managed_activity("la_live", "other", 1)
    assert not await events.note_managed_activity("la_live", "ws_live", 5)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE logical_agent_work_sessions SET state='expired',ended_at=?"
            " WHERE work_session_id='ws_live'",
            (utc_text(),),
        )
    assert not await events.note_managed_activity("la_live", "ws_live", 1)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM mcp_event_managed_activity").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_foreign_origin_explicit_attach_on_this_node_only(managed):
    path, events = managed
    seed_managed(path, authority=NODE, origin="firstbyte")
    await events.pump()
    assert rows(path, "agent.attached") == []
    assert await events.note_local_attach("la_live", "ws_live", 1, agent_name="Visible Agent")
    assert not await events.note_local_attach("la_live", "ws_live", 1)
    assert len(rows(path, "agent.attached")) == 1
    assert rows(path, "agent.attached")[0]["data"]["server"] == NODE


@pytest.mark.asyncio
async def test_remote_authority_explicit_attach_fails_closed(managed):
    path, events = managed
    seed_managed(path, authority="firstbyte", origin="firstbyte")
    assert not await events.note_local_attach("la_live", "ws_live", 1)
    assert rows(path, "agent.attached") == []


@pytest.mark.asyncio
async def test_end_without_actual_attach_is_not_local_event(managed):
    path, events = managed
    seed_managed(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE logical_agent_work_sessions SET state='expired',ended_at=? "
            "WHERE work_session_id='ws_live'",
            (utc_text(),),
        )
    await events.pump()
    assert rows(path, "session.ended") == []
    assert rows(path, "agent.attached") == []
