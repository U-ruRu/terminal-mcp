"""Isolated state-transition reasons, resumed windows and durable event dedup."""

import json
import sqlite3
from datetime import timedelta

import pytest
from jsonschema import Draft202012Validator
from test_access_mesh_native_lifecycle import T0, actor
from test_access_mesh_native_lifecycle import fixture as _native_fixture

from terminal_mcp.core.access_mesh_grants import SlotPolicy
from terminal_mcp.core.orchestration import parse_utc, utc_text
from terminal_mcp.core.session_end_reasons import session_end_reason
from terminal_mcp.mcp.event_store import MCPEventStore, event_catalog

reason_fixture = _native_fixture


def ended_records(path):
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT event_id,source_key,occurred_at,data_json FROM mcp_event_records "
            "WHERE name='session.ended' ORDER BY rowid"
        ).fetchall()
    return [
        {"id": row[0], "source": row[1], "timestamp": row[2], "data": json.loads(row[3])}
        for row in rows
    ]


def session_row(f, identity):
    with sqlite3.connect(f.store.path) as db:
        return db.execute(
            "SELECT state,end_reason,hard_expires_at FROM logical_agent_work_sessions "
            "WHERE work_session_id=?", (identity["work_session_id"],),
        ).fetchone()


async def arrange(f):
    events = MCPEventStore(f.store.path, "firstbyte")
    await events.initialize()

    async def attached(*args, **kwargs):
        return await events.note_local_attach(*args, now=f.clock[0], **kwargs)

    f.app.attach_notifier = attached
    f.app.defaults = SlotPolicy(10, 5, False)
    issued = await f.app.issuer_session(actor("access", "worker"), action="start")
    slot = f.store.numbers.winner(issued["session_number"])
    issued["slot_id"] = slot["slot_id"]
    identity = await f.app.attach(
        actor("executor", "worker"), issuer_node_id="firstbyte",
        access_code=issued["session_number"],
    )
    await events.pump(now=f.clock[0])
    return events, issued, identity


@pytest.mark.parametrize(
    "stored,expected",
    [
        # One value per normalization branch plus the null fallback. Legacy
        # spellings share the same set-membership branch.
        ("manual", "manual"),
        ("expired", "expired"),
        ("SlotSuspended", "SlotSuspended"),
        (None, "unknown"),
    ],
)
def test_other_committed_causes_are_not_mislabeled(stored, expected):
    assert session_end_reason(stored) == expected


@pytest.mark.parametrize("stored", ["local_cycle_ended", "expired_session_number_recycled"])
def test_deadline_candidate_requires_actual_elapsed_deadline(stored):
    deadline = utc_text(T0 + timedelta(seconds=10))
    assert session_end_reason(
        stored, ended_at=utc_text(T0 + timedelta(seconds=9)), hard_expires_at=deadline
    ) == stored
    assert session_end_reason(
        stored, ended_at=deadline, hard_expires_at=deadline
    ) == "expired"
    assert session_end_reason(stored, ended_at="invalid", hard_expires_at=deadline) == stored
    assert session_end_reason(stored, ended_at=deadline) == stored


def test_reason_schema_changes_only_session_ended():
    events = {row["name"]: row for row in event_catalog()["events"]}
    ended = events["session.ended"]["payloadSchema"]
    assert "reason" not in ended["required"]
    assert ended["properties"]["reason"]["type"] == "string"
    for name in ("agent.attached", "agent.inactive", "message.received"):
        assert "reason" not in events[name]["payloadSchema"]["properties"]
    validator = Draft202012Validator(ended)
    for reason in ("manual", "expired", "SlotSuspended", "unknown"):
        validator.validate({"agent_name": "Alpha-1 - Worker", "server": "firstbyte",
                            "reason": reason})
    validator.validate({"agent_name": "Alpha-1", "server": "firstbyte"})


@pytest.mark.asyncio
async def test_explicit_access_end_persists_manual_at_actual_transition(reason_fixture):
    f = reason_fixture
    events, _, identity = await arrange(f)
    f.clock[0] = T0 + timedelta(seconds=2)
    assert (await f.app.issuer_session(actor("access", "worker"), action="end"))["ok"]
    assert session_row(f, identity)[:2] == ("ended", "manual")
    with sqlite3.connect(f.store.path) as db:
        sources = db.execute(
            "SELECT payload_json FROM instance_events WHERE event_type='work_session.changed' "
            "AND entity_id=? ORDER BY seq", (identity["work_session_id"],),
        ).fetchall()
        assert any(json.loads(row[0])["end_reason"] == "manual" for row in sources)
    await events.pump(now=f.clock[0])
    rows = ended_records(f.store.path)
    assert len(rows) == 1
    assert rows[0]["data"] == {
        "agent_name": identity["public_name"], "server": "firstbyte", "reason": "manual",
    }
    assert parse_utc(rows[0]["timestamp"]) == f.clock[0]


@pytest.mark.asyncio
async def test_timer_expiry_is_not_manual_and_stays_one_event(reason_fixture):
    f = reason_fixture
    events, _, identity = await arrange(f)
    f.clock[0] = T0 + timedelta(seconds=9)
    await f.app.tick()
    await events.pump(now=f.clock[0])
    assert ended_records(f.store.path) == []
    f.clock[0] = T0 + timedelta(seconds=10)
    await f.app.tick()
    await events.pump(now=f.clock[0])
    assert session_row(f, identity)[:2] == ("ended", "expired")
    first = ended_records(f.store.path)
    assert len(first) == 1 and first[0]["data"]["reason"] == "expired"
    assert parse_utc(first[0]["timestamp"]) == f.clock[0]
    await f.app.tick()
    await events.pump(now=f.clock[0] + timedelta(seconds=1))
    assert ended_records(f.store.path) == first


@pytest.mark.asyncio
async def test_manual_end_resume_then_expiry_has_distinct_correct_causes(reason_fixture):
    f = reason_fixture
    events, issued, original = await arrange(f)
    original_deadline = session_row(f, original)[2]
    f.clock[0] = T0 + timedelta(seconds=2)
    await f.app.issuer_session(actor("access", "worker"), action="end")
    await events.pump(now=f.clock[0])
    manual = ended_records(f.store.path)
    f.clock[0] = T0 + timedelta(seconds=3)
    assert (await f.app.issuer_session(actor("access", "worker"), action="start"))["ok"]
    resumed = await f.app.attach(
        actor("executor", "worker"), issuer_node_id="firstbyte",
        access_code=issued["session_number"]
    )
    assert resumed["work_session_id"] != original["work_session_id"]
    assert resumed["session_epoch"] > original["session_epoch"]
    assert session_row(f, resumed)[2] == original_deadline
    f.clock[0] = T0 + timedelta(seconds=10)
    await f.app.tick()
    await events.pump(now=f.clock[0])
    rows = ended_records(f.store.path)
    assert [row["data"]["reason"] for row in rows] == ["manual", "expired"]
    assert rows[0] == manual[0] and rows[0]["id"] != rows[1]["id"]
    assert rows[0]["data"]["agent_name"] == rows[1]["data"]["agent_name"]
    await f.app.tick()
    await events.pump(now=f.clock[0])
    assert ended_records(f.store.path) == rows


@pytest.mark.asyncio
async def test_journal_snapshot_not_latest_cleanup_metadata_determines_reason(reason_fixture):
    f = reason_fixture
    events, _, identity = await arrange(f)
    f.clock[0] = T0 + timedelta(seconds=2)
    await f.app.issuer_session(actor("access", "worker"), action="end")
    # A later retry changes mutable metadata before the first materialization.
    with sqlite3.connect(f.store.path) as db:
        db.execute(
            "UPDATE logical_agent_work_sessions SET end_reason='retry_cleanup' "
            "WHERE work_session_id=?", (identity["work_session_id"],),
        )
    await events.pump(now=f.clock[0])
    first = ended_records(f.store.path)
    assert len(first) == 1 and first[0]["data"]["reason"] == "manual"
    with sqlite3.connect(f.store.path) as db:
        db.execute("UPDATE mcp_event_cursor SET last_seq=0 WHERE singleton=1")
    await events.pump(now=f.clock[0])
    assert ended_records(f.store.path) == first


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["SlotSuspended", "SlotDeleted"])
async def test_other_mesh_transition_reason_is_preserved(reason_fixture, kind):
    f = reason_fixture
    events, issued, identity = await arrange(f)
    f.clock[0] = T0 + timedelta(seconds=2)
    await f.app.change(actor("access", "worker"), slot_id=issued["slot_id"], kind=kind,
                       expected_revision=1)
    assert session_row(f, identity)[:2] == ("ended", kind)
    await events.pump(now=f.clock[0])
    assert ended_records(f.store.path)[0]["data"]["reason"] == kind
