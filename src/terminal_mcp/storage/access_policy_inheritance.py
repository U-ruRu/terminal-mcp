"""Atomic issuer-default changes and replicated next-window policy propagation.

The Fleet control store remains the only Mesh policy authority. This module stores
its applied parent revision and the issuer's explicit override, then appends normal
Access events in the SAME SQLite transaction as the defaults update. Readers can
never observe a new default alongside half-updated inherited slots.
"""

from __future__ import annotations

import json
import secrets

from terminal_mcp.core.access_mesh_grants import AccessMeshError, AccessSlotEvent, SlotPolicy


def _write(db, *, revision, policy, inherit_policy, parent, legacy_admission_enabled):
    db.execute(
        "INSERT INTO access_mesh_operator_defaults "
        "(singleton,revision,policy_json,inherit_policy,parent_json,legacy_admission_enabled) "
        "VALUES(1,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET "
        "revision=excluded.revision,policy_json=excluded.policy_json,"
        "inherit_policy=excluded.inherit_policy,"
        "parent_json=excluded.parent_json,legacy_admission_enabled=excluded.legacy_admission_enabled",
        (
            revision,
            json.dumps(policy, sort_keys=True),
            int(inherit_policy),
            json.dumps(parent, sort_keys=True) if parent else None,
            int(legacy_admission_enabled),
        ),
    )


def _propagate(store, db, policy):
    if store.registry_authority_id != store.local_node_id:
        return
    if (
        store.registry_mesh_id
        and db.execute(
            "SELECT upgrade_pending FROM access_mesh_registry_scope WHERE singleton=1"
        ).fetchone()[0]
    ):
        # The old issuers can hold later revisions absent from this primary.
        # Do not manufacture competing revisions before their registry union.
        return
    resolved = SlotPolicy(**policy)
    # Bounded pages, indexed issuer selection; no network while a transaction is open.
    after = ""
    recipients = tuple(sorted(store.trusted_issuers - {store.local_node_id}))
    now = store.clock()
    while True:
        rows = db.execute(
            "SELECT * FROM access_mesh_slot_replicas WHERE slot_id>? "
            "AND inherit_policy=1 "
            "ORDER BY slot_id LIMIT 100",
            (after,),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            slot = store._snapshot(row, _connection=db)
            if slot.policy == resolved:
                continue
            event = AccessSlotEvent(
                issuer_id=slot.issuer_id,
                slot_id=slot.slot_id,
                logical_agent_id=None,
                revision=slot.revision + 1,
                event_id="ae_" + secrets.token_urlsafe(18),
                kind="SlotPolicyChanged",
                policy=resolved,
                inherit_policy=True,
                policy_window=store.capture_policy_window(slot, now),
            )
            store.apply_event(
                event,
                authenticated_peer_id=store.local_node_id,
                broadcast_to=recipients,
                _connection=db,
            )
        after = rows[-1]["slot_id"]


def _notify(store):
    # Wake-up failure is not a failed durable mutation. Periodic replication reads the outbox.
    if store._delivery_notifier is not None:
        try:
            store._delivery_notifier()
        except Exception:
            pass


def change_defaults(
    store,
    *,
    expected_revision,
    policy,
    receipt,
    inherit_policy=False,
    parent=None,
    legacy_admission_enabled=False,
):
    SlotPolicy(**policy)
    if type(legacy_admission_enabled) is not bool:
        raise AccessMeshError("access_mesh_invalid_policy")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        store.assert_registry_ready(db)
        row = db.execute(
            "SELECT revision FROM access_mesh_operator_defaults WHERE singleton=1"
        ).fetchone()
        revision = row[0] if row else 1
        if revision != expected_revision:
            raise AccessMeshError("revision_conflict")
        _write(
            db,
            revision=revision + 1,
            policy=policy,
            inherit_policy=inherit_policy,
            parent=parent,
            legacy_admission_enabled=legacy_admission_enabled,
        )
        _propagate(store, db, policy)
        if receipt is not None:
            store._save_receipt_tx(db, receipt)
    _notify(store)
    return store.defaults()


def sync_parent_defaults(store, parent):
    """Apply a trusted local Fleet replica; preserve existing explicit server overrides."""
    SlotPolicy(**parent["policy"])
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        store.assert_registry_ready(db)
        row = db.execute("SELECT * FROM access_mesh_operator_defaults WHERE singleton=1").fetchone()
        inherited = parent["source"] == "mesh" or row is None or bool(row["inherit_policy"])
        policy = parent["policy"] if inherited else json.loads(row["policy_json"])
        legacy = (
            bool(parent.get("legacy_admission_enabled", False))
            if inherited
            else bool(row["legacy_admission_enabled"])
        )
        old_parent = json.loads(row["parent_json"]) if row and row["parent_json"] else None
        if row is not None and old_parent == parent and bool(row["inherit_policy"]) == inherited:
            return {
                "revision": row["revision"],
                "policy": policy,
                "inherit_policy": inherited,
                "parent": parent,
                "legacy_admission_enabled": legacy,
            }
        revision = int(row["revision"]) + 1 if row else 2
        _write(
            db,
            revision=revision,
            policy=policy,
            inherit_policy=inherited,
            parent=parent,
            legacy_admission_enabled=legacy,
        )
        _propagate(store, db, policy)
    _notify(store)
    return store.defaults()
