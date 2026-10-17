"""One-time Access data conversion and common Mesh policy ownership."""

import json
import sqlite3
from dataclasses import asdict

from test_access_mesh_registry_storage import registry

from terminal_mcp.core.access_mesh_grants import LocalAccessMesh, SlotPolicy


def test_temporary_kind_migration_preserves_slot_and_attachment(tmp_path):
    path = tmp_path / "old.db"
    policy = json.dumps(asdict(SlotPolicy(10, 5)))
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE access_mesh_slot_replicas (
                issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL, logical_agent_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('legacy','persistent')),
                state TEXT NOT NULL, revision INTEGER NOT NULL, event_id TEXT NOT NULL,
                policy_json TEXT NOT NULL, code_tag TEXT NOT NULL, anchor TEXT,
                PRIMARY KEY(issuer_id,slot_id)
            ) WITHOUT ROWID;
            CREATE TABLE access_mesh_attachments (
                connection_key TEXT PRIMARY KEY, issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL,
                logical_agent_id TEXT NOT NULL, FOREIGN KEY(issuer_id,slot_id)
                REFERENCES access_mesh_slot_replicas(issuer_id,slot_id)
            );
        """)
        db.execute(
            "INSERT INTO access_mesh_slot_replicas VALUES"
            "('a','slot','agent','legacy','active',3,'event-3',?,'tag',NULL)",
            (policy,),
        )
        db.execute("INSERT INTO access_mesh_attachments VALUES('binding','a','slot','agent')")
    store = LocalAccessMesh(
        path,
        local_node_id="a",
        trusted_issuers=frozenset({"a"}),
        proof_key=b"migration-tests-only-proof-32bytes",
    )
    slot = store.slot("a", "slot")
    assert (slot.kind, slot.revision, slot.logical_agent_id, slot.code_tag) == (
        "temporary",
        3,
        "agent",
        "tag",
    )
    with store._connect() as db:
        assert tuple(db.execute("SELECT * FROM access_mesh_attachments").fetchone()) == (
            "binding",
            "a",
            "slot",
            "agent",
        )
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None


def test_mesh_parent_replaces_a_previous_node_default_override(tmp_path):
    store = registry(tmp_path / "registry.db")
    local = asdict(SlotPolicy(60, 10))
    mesh = asdict(SlotPolicy(120, 20))
    store.change_defaults(
        expected_revision=1,
        policy=local,
        receipt=None,
        inherit_policy=False,
        parent={"source": "server", "policy": local, "revision": 1},
    )
    store.install_registry_scope(mesh_id="mesh-x", authority_id="a")
    store.sync_parent_defaults(
        {"source": "mesh", "policy": mesh, "revision": 7, "mesh_id": "mesh-x"}
    )
    assert store.defaults()["policy"] == mesh
    assert store.defaults()["inherit_policy"]
