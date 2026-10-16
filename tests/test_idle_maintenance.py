"""Small regressions for avoiding unchanged background reconciliation."""

import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.core.persistent_lifecycle import PersistentLifecycleCoordinator
from terminal_mcp.storage.access_mesh import AccessMeshStore
from terminal_mcp.storage.persistent_agents import PersistentAgentStore

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


@pytest.fixture
def due_store(tmp_path):
    path = tmp_path / "due.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE logical_agents (
                logical_agent_id TEXT PRIMARY KEY,display_name TEXT,state TEXT,
                authority_node_id TEXT,authority_epoch INTEGER,slot_revision INTEGER,
                selector_generation INTEGER,auth_generation INTEGER,created_at TEXT,
                updated_at TEXT,deleted_at TEXT,tombstone_reason TEXT);
            CREATE TABLE logical_agent_work_sessions (
                logical_agent_id TEXT,state TEXT,hard_expires_at TEXT);
            CREATE TABLE logical_agent_work_windows (
                logical_agent_id TEXT,superseded_at TEXT);
            INSERT INTO logical_agents VALUES
                ('a','A','active','local',1,1,1,1,'2026-01-01','2026-01-01',NULL,NULL);
            INSERT INTO logical_agent_work_sessions VALUES
                ('a','active','2026-10-16T12:00:00Z');
        """)
    return PersistentAgentStore(path)


@pytest.mark.parametrize(
    ("sql", "selected"),
    [
        ("SELECT 1", True),
        ("UPDATE logical_agent_work_sessions SET hard_expires_at='2026-10-17T12:00:00Z'", False),
        (
            "UPDATE logical_agent_work_sessions SET state='stopping', "
            "hard_expires_at='2026-10-17T12:00:00Z'",
            True,
        ),
        ("UPDATE logical_agents SET authority_node_id='remote'", False),
        ("UPDATE logical_agents SET state='deleted'", False),
        ("INSERT INTO logical_agent_work_windows VALUES('a',NULL)", False),
        ("UPDATE logical_agent_work_sessions SET state='ended'", False),
    ],
    ids=["deadline-boundary", "future", "stopping", "foreign", "deleted", "managed", "ended"],
)
async def test_due_query_eligibility(due_store, sql, selected):
    with sqlite3.connect(due_store.path) as db:
        db.execute(sql)
    rows = await due_store.due_session_slots(authority_node_id="local", now=NOW.isoformat())
    assert [row.logical_agent_id for row in rows] == (["a"] if selected else [])


async def test_empty_expiry_pass_never_visits_historical_slots():
    store = SimpleNamespace(due_session_slots=AsyncMock(return_value=[]))
    lifecycle = PersistentLifecycleCoordinator(
        store, enabled=True, authority_node_id="local", session_duration_seconds=120
    )
    assert await lifecycle.reconcile_expired(now=NOW) == []


def maintenance():
    # Only the maintenance driver's ports are needed, not the app/auth/server.
    app = object.__new__(AccessMeshApplication)
    app.clock = Mock(return_value=NOW)
    app.store = SimpleNamespace(lifecycle_signature=Mock(return_value=("same",)))
    app._maintenance_snapshot = ("same",)
    app._maintenance_observed_at = NOW
    app._maintenance_deadline = NOW + timedelta(seconds=10)
    app._scan_after = ""
    app.last_error = None
    app.tick = AsyncMock()
    app.cleanup = AsyncMock()
    return app


async def test_unchanged_cycle_skips_materialization():
    app = maintenance()
    await app._maintenance_tick()
    app.tick.assert_not_awaited()


async def test_idle_cycle_keeps_cleanup_retry():
    app = maintenance()
    await app._maintenance_tick()
    app.cleanup.assert_awaited_once()


@pytest.mark.parametrize("reason", ["changed", "deadline", "retry", "clock-reversed"])
async def test_maintenance_resumes_for_one_required_trigger(reason):
    app = maintenance()
    if reason == "changed":
        app.store.lifecycle_signature.return_value = ("changed",)
    elif reason == "deadline":
        app._maintenance_deadline = NOW
    elif reason == "retry":
        app.last_error = "temporary_failure"
    else:
        app._maintenance_observed_at = NOW + timedelta(seconds=1)
    await app._maintenance_tick()
    app.tick.assert_awaited_once()


async def test_later_page_does_not_acknowledge_changes_to_earlier_page():
    app = maintenance()
    app._scan_after = "page-one-end"
    app.store.lifecycle_signature.return_value = ("changed",)
    await app._maintenance_tick()
    assert app._maintenance_snapshot == ("same",)


@pytest.fixture
def signature_store(tmp_path):
    # The signature needs relational keys and changing values, not auth/app setup.
    store = object.__new__(AccessMeshStore)
    store.path = str(tmp_path / "signature.sqlite3")
    store.trusted_issuers = frozenset({"local"})
    with sqlite3.connect(store.path) as db:
        db.executescript("""
            CREATE TABLE access_mesh_attachments(issuer_id,slot_id,logical_agent_id);
            CREATE TABLE access_mesh_slot_replicas(issuer_id,slot_id,revision);
            CREATE TABLE access_mesh_number_claims(issuer_id,slot_id,number);
            CREATE TABLE access_mesh_number_starts(number,event_id);
            CREATE TABLE access_mesh_number_ends(number,event_id);
            CREATE TABLE access_mesh_local_sessions(issuer_id,slot_id,work_session_id);
            CREATE TABLE logical_agent_work_sessions(work_session_id,state);
            CREATE TABLE access_mesh_cleanup_jobs(job_id,logical_agent_id,work_session_id,
                session_epoch,reason,release_claims,completed_at);
            CREATE TABLE commands(command);
            INSERT INTO access_mesh_attachments VALUES('local','s','a');
            INSERT INTO access_mesh_slot_replicas VALUES('local','s',1);
            INSERT INTO access_mesh_number_claims VALUES('local','s','0012');
            INSERT INTO access_mesh_local_sessions VALUES('local','s','w');
            INSERT INTO logical_agent_work_sessions VALUES('w','active');
        """)
    return store


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE access_mesh_slot_replicas SET revision=2",
        "INSERT INTO access_mesh_number_starts VALUES('0012','start')",
        "INSERT INTO access_mesh_number_ends VALUES('0012','end')",
        "UPDATE logical_agent_work_sessions SET state='ended'",
    ],
    ids=["policy-revision", "manual-start", "shared-end", "cleanup-completed"],
)
def test_lifecycle_signature_observes_an_external_transition(signature_store, mutation):
    before = signature_store.lifecycle_signature()
    with sqlite3.connect(signature_store.path) as db:
        db.execute(mutation)
    assert signature_store.lifecycle_signature() != before


def test_lifecycle_signature_ignores_command_history(signature_store):
    before = signature_store.lifecycle_signature()
    with sqlite3.connect(signature_store.path) as db:
        db.execute("INSERT INTO commands VALUES('unrelated history')")
    assert signature_store.lifecycle_signature() == before


def test_lifecycle_signature_ignores_unattached_unrelated_slots(signature_store):
    before = signature_store.lifecycle_signature()
    with sqlite3.connect(signature_store.path) as db:
        db.execute("INSERT INTO access_mesh_slot_replicas VALUES('remote','unrelated',1)")
    assert signature_store.lifecycle_signature() == before


def test_lifecycle_signature_watches_unattached_number_group_members(signature_store):
    with sqlite3.connect(signature_store.path) as db:
        db.execute("INSERT INTO access_mesh_slot_replicas VALUES('remote','alias',1)")
        db.execute("INSERT INTO access_mesh_number_claims VALUES('remote','alias','0012')")
    before = signature_store.lifecycle_signature()
    with sqlite3.connect(signature_store.path) as db:
        db.execute("UPDATE access_mesh_slot_replicas SET revision=2 WHERE issuer_id='remote'")
    assert signature_store.lifecycle_signature() != before
