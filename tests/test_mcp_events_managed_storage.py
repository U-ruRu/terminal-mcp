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
async def test_verified_local_attach_records_the_managed_agent(managed):
    path, events = managed
    seed_managed(path)

    await events.note_local_attach("la_live", "ws_live", 1, agent_name="Visible Agent")

    attached = rows(path, "agent.attached")
    assert len(attached) == 1
    assert attached[0]["data"] == {"agent_name": "Visible Agent", "server": NODE}


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["ended", "expired"])
async def test_end_event_uses_the_committed_cause_before_metadata_cleanup(managed, terminal):
    path, events = managed
    seed_managed(path)
    await events.note_local_attach("la_live", "ws_live", 1)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE logical_agent_work_sessions SET state=?,ended_at=?,end_reason=? "
            "WHERE work_session_id='ws_live'",
            (terminal, utc_text(), "session_end" if terminal == "ended" else "hard_duration"),
        )
        db.execute(
            "UPDATE logical_agent_work_sessions SET end_reason='retry_cle"
            "anup' WHERE work_session_id='ws_live'"
        )

    await events.pump()

    ended = rows(path, "session.ended")
    assert len(ended) == 1
    assert ended[0]["data"] == {
        "agent_name": "Visible Agent",
        "server": NODE,
        "reason": "manual" if terminal == "ended" else "expired",
    }


@pytest.mark.asyncio
async def test_managed_inactivity_emits_after_180_seconds(managed):
    path, events = managed
    start = parse_utc(seed_managed(path))
    await events.note_local_attach("la_live", "ws_live", 1, now=start)

    await events.pump(now=start + timedelta(seconds=181))

    inactive = rows(path, "agent.inactive")
    assert len(inactive) == 1
    assert inactive[0]["data"] == {"agent_name": "Visible Agent", "server": NODE}


@pytest.mark.asyncio
async def test_exact_180_seconds_has_not_reached_the_inactivity_threshold(managed):
    path, events = managed
    start = parse_utc(seed_managed(path))
    await events.note_local_attach("la_live", "ws_live", 1, now=start)

    await events.pump(now=start + timedelta(seconds=180))

    assert rows(path, "agent.inactive") == []


@pytest.mark.asyncio
async def test_an_already_notified_activity_episode_is_not_emitted_again(managed):
    path, events = managed
    started = seed_managed(path)
    start = parse_utc(started)
    await events.note_local_attach("la_live", "ws_live", 1, now=start)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO mcp_event_inactivity VALUES('managed:ws_live',?)", (started,))

    await events.pump(now=start + timedelta(seconds=181))

    assert rows(path, "agent.inactive") == []


@pytest.mark.asyncio
async def test_new_activity_gets_its_own_inactivity_event(managed):
    path, events = managed
    started = seed_managed(path)
    start = parse_utc(started)
    await events.note_local_attach("la_live", "ws_live", 1, now=start)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO mcp_event_inactivity VALUES('managed:ws_live',?)", (started,))
    latest = start + timedelta(seconds=200)
    await events.note_managed_activity("la_live", "ws_live", 1, now=latest)

    await events.pump(now=latest + timedelta(seconds=181))

    inactive = rows(path, "agent.inactive")
    assert len(inactive) == 1
    assert inactive[0]["source_key"].endswith(utc_text(latest))


@pytest.mark.asyncio
async def test_remote_authority_explicit_attach_fails_closed(managed):
    path, events = managed
    seed_managed(path, authority="firstbyte", origin="firstbyte")
    assert not await events.note_local_attach("la_live", "ws_live", 1)
    assert rows(path, "agent.attached") == []
