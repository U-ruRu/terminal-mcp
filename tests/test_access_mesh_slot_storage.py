"""Slot ownership, physical deletion and retention at the owning SQLite boundary."""

from datetime import UTC, datetime, timedelta

import pytest

from terminal_mcp.core.access_mesh_grants import AccessSlotEvent, SlotPolicy
from terminal_mcp.core.human_names import number_agent_name
from terminal_mcp.storage.access_mesh import AccessMeshStore

T0 = datetime(2026, 10, 18, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    result = AccessMeshStore(
        tmp_path / "slots.db",
        local_node_id="node",
        trusted_issuers=frozenset({"node"}),
        proof_key=b"slot-storage-fixture-proof-32bytes",
        clock=lambda: T0,
    )
    # Native persistence is part of this adapter's transaction. Only its owned
    # columns are needed; application/auth/transport neighbors stay out of scope.
    with result._connect() as db:
        db.executescript("""
            CREATE TABLE logical_agents(
                logical_agent_id TEXT PRIMARY KEY,display_name TEXT,
                state TEXT,authority_node_id TEXT,
                authority_epoch INTEGER,slot_revision INTEGER,selector_generation INTEGER,
                auth_generation INTEGER,created_at TEXT,updated_at TEXT,
                deleted_at TEXT,tombstone_reason TEXT
            );
            CREATE TABLE logical_agent_work_sessions(
                work_session_id TEXT PRIMARY KEY,logical_agent_id TEXT,session_epoch INTEGER,
                authority_node_id TEXT,authority_epoch INTEGER,started_at TEXT,hard_expires_at TEXT,
                auth_principal_id TEXT,auth_generation INTEGER,state TEXT,origin_instance_id TEXT,
                ended_at TEXT,end_reason TEXT,
                FOREIGN KEY(logical_agent_id) REFERENCES logical_agents(logical_agent_id)
            );
            CREATE TABLE logical_agent_rearms(
                logical_agent_id TEXT PRIMARY KEY,work_session_id TEXT,
                FOREIGN KEY(work_session_id) REFERENCES logical_agent_work_sessions(work_session_id)
            );
        """)
    return result


def issue(store, *, kind="persistent", start=False, duration=100, slot_id="slot"):
    event = AccessSlotEvent(
        issuer_id="node",
        slot_id=slot_id,
        logical_agent_id="agent" if start else None,
        revision=1,
        event_id=f"{slot_id}-issue",
        kind="SlotIssued",
        policy=SlotPolicy(duration, 60),
        code_tag=store.code_tag("node", "1234"),
        effective_at=T0 if start else None,
        created_at=T0,
        agent_name=number_agent_name("1234"),
    )
    store.apply_event(
        event,
        authenticated_peer_id="node",
        issued_kind=kind,
        mutation_number={"number": "1234", "attempt_id": "attempt", "started_at": T0},
    )
    return store.slot("node", slot_id)


def test_manual_slot_has_no_agent_identity(store):
    slot = issue(store)

    assert slot.session is None
    with store._connect() as db:
        assert "logical_agent_id" not in {
            row[1] for row in db.execute("PRAGMA table_info(access_mesh_slot_replicas)")
        }
        assert db.execute("SELECT 1 FROM access_mesh_sessions").fetchone() is None


def test_expired_slot_keeps_its_number(store):
    issue(store, start=True)

    with store._connect() as db:
        assert store.numbers._occupied(
            db, "1234", "other", as_of=(T0 + timedelta(days=2)).isoformat()
        )


def test_delete_physically_removes_native_sessions_and_frees_number(store):
    slot = issue(store, start=True)
    store.attach(issuer_id="node", code="1234", connection_key="connection")
    identity = store.local_identity(slot, now=T0)
    with store._connect() as db:
        db.execute(
            "INSERT INTO logical_agent_rearms VALUES('agent',?)", (identity["work_session_id"],)
        )
        db.execute(
            "INSERT INTO access_mesh_issuer_bindings VALUES('issuer-connection','node','slot')"
        )

    result = store.apply_event(
        AccessSlotEvent("node", "slot", None, 2, "delete", "SlotDeleted"),
        authenticated_peer_id="node",
    )

    assert result.outcome == "applied"
    with store._connect() as db:
        for table in (
            "access_mesh_slot_replicas",
            "access_mesh_sessions",
            "access_mesh_attachments",
            "access_mesh_issuer_bindings",
            "access_mesh_local_sessions",
            "access_mesh_number_claims",
            "logical_agent_work_sessions",
            "logical_agent_rearms",
            "access_mesh_slot_catalog",
        ):
            assert db.execute(f"SELECT 1 FROM {table}").fetchone() is None, table
        assert not store.numbers._occupied(db, "1234", "new")


@pytest.mark.parametrize("kind", ["temporary", "persistent"])
@pytest.mark.parametrize("seconds,eligible", [(72 * 3600 - 1, False), (72 * 3600, True)])
def test_unused_manual_slot_retention_boundary(store, kind, seconds, eligible):
    slot = issue(store, kind=kind)

    expired = store.expired_unused_slots(now=T0 + timedelta(seconds=seconds))

    assert bool(expired) is eligible
    if eligible:
        assert expired == [slot]


@pytest.mark.parametrize("hours,eligible", [(151, False), (152, True)])
def test_unmaterialized_long_session_retention_counts_from_deadline(store, hours, eligible):
    issue(store, start=True, duration=80 * 3600)

    assert bool(store.expired_unused_slots(now=T0 + timedelta(hours=hours))) is eligible


def test_retention_delete_rechecks_recent_touch_in_its_transaction(store):
    slot = issue(store)
    now = T0 + timedelta(hours=73)
    store.touch_slot(slot.issuer_id, slot.slot_id, now=now)

    result = store.apply_event(
        AccessSlotEvent("node", "slot", None, 2, "stale-scan-delete", "SlotDeleted"),
        authenticated_peer_id="node",
        retention_before=now - timedelta(hours=72),
    )

    assert result.outcome == "stale"
    assert store.slot("node", "slot") is not None


def test_natural_expiry_uses_deadline_as_native_end_time(store):
    slot = issue(store, start=True)
    identity = store.local_identity(slot, now=T0)

    store.local_identity(slot, now=T0 + timedelta(hours=2))

    with store._connect() as db:
        ended_at = db.execute(
            "SELECT ended_at FROM logical_agent_work_sessions WHERE work_session_id=?",
            (identity["work_session_id"],),
        ).fetchone()[0]
    assert datetime.fromisoformat(ended_at) == T0 + timedelta(seconds=100)


def test_late_recipient_hold_cannot_reserve_a_deleted_slots_number(store):
    issue(store, start=True)
    store.apply_event(
        AccessSlotEvent("node", "slot", None, 2, "delete", "SlotDeleted"),
        authenticated_peer_id="node",
    )
    hold = {
        "source_node_id": "node",
        "hold_id": "late",
        "recipient_agent_id": "agent",
        "issuer_id": "node",
        "slot_id": "slot",
        "number": "1234",
        "state": "held",
        "revision": 1,
    }

    store.recipient_holds.merge([hold], authenticated_peer_id="node")

    with store._connect() as db:
        assert not store.numbers._occupied(db, "1234", "new")


def test_snapshot_page_keeps_slot_and_session_in_one_read_transaction(store, monkeypatch):
    issue(store, start=True)
    with store._connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
    snapshot_wire = store.snapshot_wire

    def end_after_slot_read(row, **kwargs):
        store.apply_event(
            AccessSlotEvent("node", "slot", None, 2, "end", "SessionEnded", effective_at=T0),
            authenticated_peer_id="node",
        )
        return snapshot_wire(row, **kwargs)

    monkeypatch.setattr(store, "snapshot_wire", end_after_slot_read)

    page = store.snapshot_page(issuer_id="node")

    assert page[0]["revision"] == 1
    assert page[0]["session"]["active"] is True


@pytest.mark.parametrize("started", [False, True])
def test_suspended_slot_retention_uses_its_last_authorized_activity(store, started):
    issue(store, start=started, duration=80 * 3600)
    store.apply_event(
        AccessSlotEvent(
            "node",
            "slot",
            None,
            2,
            "suspend",
            "SlotSuspended",
            effective_at=T0 + timedelta(hours=1),
        ),
        authenticated_peer_id="node",
    )
    now = T0 + timedelta(hours=73 if started else 72)

    expired = store.expired_unused_slots(now=now)

    assert [slot.slot_id for slot in expired] == ["slot"]
    with store._connect() as db:
        assert store._slot_unused(db, expired[0], before=now - timedelta(hours=72), now=now)
