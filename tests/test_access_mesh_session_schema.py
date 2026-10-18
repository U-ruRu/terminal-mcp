"""The single deployed v26 -> v27 session-model transition."""

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from terminal_mcp.core.access_mesh_grants import SlotPolicy
from terminal_mcp.storage.access_mesh import AccessMeshStore

T0 = datetime(2026, 10, 18, tzinfo=UTC)


def test_current_v26_upgrade_preserves_live_identity_and_physically_removes_deleted_slot(tmp_path):
    path = tmp_path / "current-v26.db"
    policy = SlotPolicy(3600, 60)
    issue = {
        "issuer_id": "node",
        "slot_id": "live",
        "logical_agent_id": "agent-live",
        "revision": 1,
        "event_id": "issued",
        "kind": "SlotIssued",
        "policy": asdict(policy),
        "code_tag": "a" * 64,
        "effective_at": T0.isoformat(),
    }
    end = {
        **issue,
        "revision": 2,
        "event_id": "ended",
        "kind": "SessionEnded",
        "policy": None,
        "code_tag": None,
        "effective_at": (T0 + timedelta(minutes=5)).isoformat(),
    }
    with sqlite3.connect(path) as db:
        db.executescript("""
            PRAGMA user_version=26;
            CREATE TABLE access_mesh_slot_replicas(
                issuer_id TEXT NOT NULL,slot_id TEXT NOT NULL,logical_agent_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('temporary','persistent')),
                state TEXT NOT NULL CHECK(state IN ('active','suspended','deleted')),
                revision INTEGER NOT NULL,event_id TEXT NOT NULL,policy_json TEXT NOT NULL,
                code_tag TEXT NOT NULL,anchor TEXT,PRIMARY KEY(issuer_id,slot_id)
            ) WITHOUT ROWID;
            CREATE INDEX access_mesh_slots_agent ON access_mesh_slot_replicas(logical_agent_id);
            CREATE TABLE access_mesh_attachments(
                connection_key TEXT PRIMARY KEY,issuer_id TEXT NOT NULL,slot_id TEXT NOT NULL,
                logical_agent_id TEXT NOT NULL,
                FOREIGN KEY(issuer_id,slot_id)
                REFERENCES access_mesh_slot_replicas(issuer_id,slot_id)
            );
            CREATE TABLE access_mesh_event_log(
                issuer_id TEXT NOT NULL,slot_id TEXT NOT NULL,revision INTEGER NOT NULL,
                event_id TEXT NOT NULL,event_json TEXT NOT NULL,
                PRIMARY KEY(issuer_id,slot_id,revision),
                UNIQUE(issuer_id,event_id)
            ) WITHOUT ROWID;
            CREATE TABLE work_items(task_id TEXT PRIMARY KEY,state TEXT,checkpoint TEXT);
            INSERT INTO work_items VALUES('task','in_progress','keep');
            CREATE TABLE access_mesh_number_claims(
                issuer_id TEXT NOT NULL,slot_id TEXT NOT NULL,logical_agent_id TEXT NOT NULL,
                number TEXT NOT NULL,started_at TEXT NOT NULL,hard_expires_at TEXT NOT NULL,
                PRIMARY KEY(issuer_id,slot_id));
            CREATE TABLE access_mesh_number_ends(
                number TEXT NOT NULL,cycle_key TEXT NOT NULL,
                issuer_id TEXT NOT NULL,slot_id TEXT NOT NULL,
                event_id TEXT NOT NULL,ended_at TEXT NOT NULL,
                PRIMARY KEY(number,cycle_key,event_id));
        """)
        db.execute(
            "INSERT INTO access_mesh_slot_replicas VALUES('node','live','agent-live','persistent',"
            "'active',2,'ended',?,?,?)",
            (json.dumps(asdict(policy)), "a" * 64, (T0 + timedelta(minutes=6)).isoformat()),
        )
        db.execute(
            "INSERT INTO access_mesh_slot_replicas VALUES('node','gone','agent-gone','temporary',"
            "'deleted',2,'deleted',?,?,NULL)",
            (json.dumps(asdict(policy)), "b" * 64),
        )
        db.execute(
            "INSERT INTO access_mesh_attachments VALUES('live-connection','node','live','ag"
            "ent-live')"
        )
        db.execute(
            "INSERT INTO access_mesh_attachments VALUES('gone-connection','node','gone','ag"
            "ent-gone')"
        )
        for event in (issue, end):
            db.execute(
                "INSERT INTO access_mesh_event_log VALUES('node','live',?,?,?)",
                (event["revision"], event["event_id"], json.dumps(event)),
            )
        db.execute(
            "INSERT INTO access_mesh_number_claims VALUES('node','live','agent-live','1234',?,?)",
            (T0.isoformat(), (T0 + timedelta(hours=1)).isoformat()),
        )
        db.execute(
            "INSERT INTO access_mesh_number_ends VALUES('1234',?,'node','live','ended',?)",
            (f"node:live:{T0.isoformat()}", end["effective_at"]),
        )

    current = AccessMeshStore(
        path,
        local_node_id="node",
        trusted_issuers=frozenset({"node"}),
        proof_key=b"current-schema-fixture-proof-32bytes",
    )

    slot = current.slot("node", "live")
    assert slot.session.logical_agent_id == "agent-live"
    assert slot.session.active is False
    assert slot.session.ended_at == T0 + timedelta(minutes=5)
    assert slot.anchor == T0 and slot.deadline_at == T0 + timedelta(hours=1)
    assert current.slot("node", "gone") is None
    with current._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 27
        assert "logical_agent_id" not in {
            row[1] for row in db.execute("PRAGMA table_info(access_mesh_slot_replicas)")
        }
        assert (
            db.execute("SELECT connection_key FROM access_mesh_attachments").fetchone()[0]
            == "live-connection"
        )
        assert tuple(db.execute("SELECT * FROM work_items").fetchone()) == (
            "task",
            "in_progress",
            "keep",
        )
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None
        assert current.numbers.end_snapshot()[0]["revision"] == 2
