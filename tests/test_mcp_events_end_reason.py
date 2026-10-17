"""Isolated state-transition reasons, resumed windows and durable event dedup."""

import json
import sqlite3

import pytest
from test_access_mesh_native_lifecycle import actor
from test_access_mesh_native_lifecycle import fixture as _native_fixture

from terminal_mcp.core.access_mesh_grants import SlotPolicy
from terminal_mcp.core.session_end_reasons import session_end_reason
from terminal_mcp.mcp.event_store import MCPEventStore

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
            "WHERE work_session_id=?",
            (identity["work_session_id"],),
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
    await f.app.attach(actor("executor", "worker"), session_number=issued["session_number"])
    identity = f.store.observed_identity(f.store.registry_slot(issued["slot_id"]), now=f.clock[0])
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
