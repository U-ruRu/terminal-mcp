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


def seed_slot(store):
    with store._connect() as db:
        db.execute(
            "INSERT INTO access_mesh_slot_replicas "
            "(issuer_id,slot_id,logical_agent_id,kind,state,revision,event_id,"
            "policy_json,code_tag) "
            "VALUES('a','slot-a','agent-a','persistent','suspended',7,'event-7',?,?)",
            (json.dumps(asdict(SlotPolicy(10, 5))), store.code_tag("a", "1234")),
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


def test_primary_handoff_preserves_slot_revision_and_provenance(tmp_path):
    source, target = registry(tmp_path / "a.db"), registry(tmp_path / "b.db", "b")
    for store in (source, target):
        store.install_registry_scope(mesh_id="mesh-x", authority_id="a")
    seed_slot(source)
    snapshot = source.export_registry()
    target.import_registry(snapshot)
    assert target.registry_slot("slot-a") == source.registry_slot("slot-a")
    assert target.registry_issuer_trusted("a")


def test_departure_clears_only_the_moving_nodes_registry(tmp_path):
    source, moving = registry(tmp_path / "a.db"), registry(tmp_path / "b.db", "b")
    for store in (source, moving):
        store.install_registry_scope(mesh_id="mesh-x", authority_id="a")
    seed_slot(source)
    moving.import_registry(source.export_registry())
    before = source.export_registry()
    moving.begin_topology_transition("move-1")
    moving.finish_topology_transition(
        "move-1", namespace_id="mesh-y", mesh_id="mesh-y", authority_id="b"
    )
    assert moving.registry_slot("slot-a") is None
    assert source.export_registry() == before


def test_handoff_cannot_import_another_mesh_namespace(tmp_path):
    source, target = registry(tmp_path / "a.db"), registry(tmp_path / "b.db", "b")
    source.install_registry_scope(mesh_id="mesh-x", authority_id="a")
    target.install_registry_scope(mesh_id="mesh-y", authority_id="b")
    with pytest.raises(AccessMeshError, match="mesh_namespace_mismatch"):
        target.import_registry(source.export_registry())
