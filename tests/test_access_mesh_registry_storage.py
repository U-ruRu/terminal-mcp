"""Namespace fences and exact registry handoff at the owning SQLite boundary."""

import json
from dataclasses import asdict

import pytest

from terminal_mcp.core.access_mesh_grants import AccessMeshError, AccessSlotEvent, SlotPolicy
from terminal_mcp.storage.access_mesh import AccessMeshStore


def registry(path, node="a"):
    return AccessMeshStore(
        path,
        local_node_id=node,
        trusted_issuers=frozenset({node}),
        proof_key=b"registry-tests-only-32-byte-proof",
    )


def seed_slot(store, *, active=False):
    with store._connect() as db:
        db.execute(
            "INSERT INTO access_mesh_slot_replicas "
            "(issuer_id,slot_id,logical_agent_id,kind,state,revision,event_id,"
            "policy_json,code_tag) "
            "VALUES('a','slot-a','agent-a','persistent','suspended',7,'event-7',?,?)",
            (json.dumps(asdict(SlotPolicy(10, 5))), store.code_tag("a", "1234")),
        )
        if active:
            db.execute(
                "UPDATE access_mesh_slot_replicas SET state='active',anchor=?",
                (store.clock().isoformat(),),
            )


def test_transition_fence_survives_restart(tmp_path):
    path = tmp_path / "registry.db"
    store = registry(path)
    store.begin_topology_transition("move-1")
    reopened = registry(path)
    with pytest.raises(AccessMeshError, match="mesh_transition_pending"):
        reopened.assert_registry_ready()


def test_frozen_registry_rejects_a_new_slot_commit(tmp_path):
    store = registry(tmp_path / "registry.db")
    store.begin_topology_transition("move-1")
    event = AccessSlotEvent(
        issuer_id="a",
        slot_id="slot-a",
        logical_agent_id="agent-a",
        revision=1,
        event_id="issue-a",
        kind="SlotIssued",
        policy=SlotPolicy(10, 5),
        code_tag=store.code_tag("a", "1234"),
    )
    with pytest.raises(AccessMeshError, match="mesh_transition_pending"):
        store.apply_event(event, authenticated_peer_id="a", issued_kind="persistent")
    assert store.registry_slot("slot-a") is None


@pytest.mark.parametrize(
    "activity", ["slot", "session", "queued", "running", "cancelling", "cleanup"]
)
def test_transition_accepts_existing_local_activity(tmp_path, activity):
    store = registry(tmp_path / "registry.db")
    with store._connect() as db:
        if activity == "slot":
            seed_slot(store)
            db.execute(
                "UPDATE access_mesh_slot_replicas SET state='active',anchor=?",
                (store.clock().isoformat(),),
            )
            query = "SELECT state,revision FROM access_mesh_slot_replicas"
        elif activity == "session":
            # The native-session neighbor is represented only by its admission columns.
            db.execute("CREATE TABLE logical_agent_work_sessions(state TEXT,hard_expires_at TEXT)")
            db.execute(
                "INSERT INTO logical_agent_work_sessions VALUES('active',?)",
                ("2999-01-01T00:00:00+00:00",),
            )
            query = "SELECT state,hard_expires_at FROM logical_agent_work_sessions"
        elif activity == "cleanup":
            db.execute(
                "INSERT INTO access_mesh_cleanup_jobs(job_id,issuer_id,slot_id,logical_agent_id,"
                "release_claims,reason,created_at) "
                "VALUES('job','a','slot-a','agent-a',0,'test','now')"
            )
            query = "SELECT completed_at FROM access_mesh_cleanup_jobs"
        else:
            db.execute("CREATE TABLE commands(status TEXT)")
            db.execute("INSERT INTO commands VALUES(?)", (activity,))
            query = "SELECT status FROM commands"
        before = tuple(db.execute(query).fetchone())
    if activity == "slot":
        assert store.merged_cycle(store.registry_slot("slot-a"), store.clock())["state"] == "active"

    store.begin_topology_transition("move-1")

    with store._connect() as db:
        assert (
            db.execute("SELECT transition_id FROM access_mesh_registry_scope").fetchone()[0]
            == "move-1"
        )
        assert tuple(db.execute(query).fetchone()) == before


@pytest.mark.parametrize("active", [False, True])
def test_primary_handoff_preserves_slot_revision_and_provenance(tmp_path, active):
    source, target = registry(tmp_path / "a.db"), registry(tmp_path / "b.db", "b")
    for store in (source, target):
        store.install_registry_scope(mesh_id="mesh-x", authority_id="a")
    seed_slot(source, active=active)
    snapshot = source.export_registry()
    target.import_registry(snapshot)
    assert target.registry_slot("slot-a") == source.registry_slot("slot-a")
    assert target.registry_issuer_trusted("a")


@pytest.mark.parametrize("active", [False, True])
def test_departure_clears_only_the_moving_nodes_registry(tmp_path, active):
    source, moving = registry(tmp_path / "a.db"), registry(tmp_path / "b.db", "b")
    for store in (source, moving):
        store.install_registry_scope(mesh_id="mesh-x", authority_id="a")
    seed_slot(source, active=active)
    moving.import_registry(source.export_registry())
    before = source.export_registry()
    moving.begin_topology_transition("move-1")
    moving.finish_topology_transition(
        "move-1", namespace_id="mesh-y", mesh_id="mesh-y", authority_id="b"
    )
    assert moving.registry_slot("slot-a") is None
    assert source.export_registry() == before


def test_departure_retires_only_local_native_authority(tmp_path):
    path = tmp_path / "moving.db"
    source, moving = registry(tmp_path / "a.db"), registry(path, "b")
    for store in (source, moving):
        store.install_registry_scope(mesh_id="mesh-x", authority_id="a")
    seed_slot(source)
    moving.import_registry(source.export_registry())
    with moving._connect() as db:
        db.executescript(
            "CREATE TABLE logical_agents(logical_agent_id TEXT PRIMARY KEY,state TEXT,"
            "deleted_at TEXT,updated_at TEXT,tombstone_reason TEXT);"
            "CREATE TABLE logical_agent_work_sessions(work_session_id TEXT PRIMARY KEY,"
            "logical_agent_id TEXT,state TEXT,ended_at TEXT,end_reason TEXT,hard_expires_at TEXT);"
        )
        db.execute("INSERT INTO logical_agents VALUES('local-agent','active',NULL,'now',NULL)")
        db.execute(
            "INSERT INTO logical_agent_work_sessions "
            "VALUES('local-session','local-agent','active',NULL,NULL,?)",
            ("2999-01-01T00:00:00+00:00",),
        )
        db.execute(
            "INSERT INTO access_mesh_native_owners(logical_agent_id,issuer_id,slot_id) "
            "VALUES('local-agent','b','local-slot')"
        )
    source_before = source.export_registry()
    moving.begin_topology_transition("move-1")
    moving.finish_topology_transition(
        "move-1", namespace_id="mesh-y", mesh_id="mesh-y", authority_id="b"
    )
    with moving._connect() as db:
        assert tuple(
            db.execute(
                "SELECT state,end_reason FROM logical_agent_work_sessions "
                "WHERE work_session_id='local-session'"
            ).fetchone()
        ) == ("ended", "mesh_namespace_departed")
        assert tuple(
            db.execute(
                "SELECT state,tombstone_reason FROM logical_agents "
                "WHERE logical_agent_id='local-agent'"
            ).fetchone()
        ) == ("deleted", "mesh_namespace_departed")
    assert moving.registry_slot("slot-a") is None
    assert source.export_registry() == source_before


def test_handoff_cannot_import_another_mesh_namespace(tmp_path):
    source, target = registry(tmp_path / "a.db"), registry(tmp_path / "b.db", "b")
    source.install_registry_scope(mesh_id="mesh-x", authority_id="a")
    target.install_registry_scope(mesh_id="mesh-y", authority_id="b")
    with pytest.raises(AccessMeshError, match="mesh_namespace_mismatch"):
        target.import_registry(source.export_registry())
