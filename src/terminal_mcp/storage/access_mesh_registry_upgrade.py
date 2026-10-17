"""One-time consolidation of pre-unified, independently issued Access records."""

from __future__ import annotations

import base64
import json
import secrets

from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.storage.access_mesh_registry import REGISTRY_TABLES

UPGRADE_TABLES = tuple(
    table
    for table in REGISTRY_TABLES
    if table not in {"access_mesh_slot_catalog", "access_mesh_operator_defaults"}
)


def record_receipt(store, db, name, details):
    db.execute(
        "INSERT OR IGNORE INTO access_mesh_migration_receipts VALUES(?,?,?,?)",
        (
            name,
            store.namespace_id,
            store.clock().isoformat(),
            json.dumps(details, sort_keys=True, separators=(",", ":")),
        ),
    )


def _clear_receipt(store, row):
    sealed = base64.b64decode(row["result_json"])
    row["result_json"] = (
        store._receipt_cipher()
        .decrypt(sealed[:12], sealed[12:], row["request_key"].encode())
        .decode()
    )


def _seal_receipt(store, row):
    nonce = secrets.token_bytes(12)
    sealed = nonce + store._receipt_cipher().encrypt(
        nonce, row["result_json"].encode(), row["request_key"].encode()
    )
    row["result_json"] = base64.b64encode(sealed).decode()


def export_page(store, *, namespace_id, table, after=0, limit=20):
    if namespace_id != store.namespace_id:
        raise AccessMeshError("mesh_namespace_mismatch")
    if table not in UPGRADE_TABLES or type(after) is not int or after < 0:
        raise AccessMeshError("access_mesh_invalid_snapshot_request")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise AccessMeshError("access_mesh_invalid_limit")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        store.assert_registry_ready(db)
        marker = "authoritative_registry_export_v1:" + namespace_id
        if not db.execute(
            "SELECT 1 FROM access_mesh_migration_receipts WHERE name=?", (marker,)
        ).fetchone():
            counts = {}
            # Freeze one consistent input before pagination. Source data stays
            # intact; encrypted receipts remain encrypted in this local copy.
            for name in UPGRADE_TABLES:
                columns = [r[1] for r in db.execute(f'PRAGMA table_info("{name}")')]
                rows = db.execute(f'SELECT * FROM "{name}" ORDER BY {",".join(columns)}').fetchall()
                counts[name] = len(rows)
                db.executemany(
                    "INSERT INTO access_mesh_registry_upgrade_export VALUES(?,?,?,?)",
                    (
                        (namespace_id, name, offset, json.dumps(dict(row), sort_keys=True))
                        for offset, row in enumerate(rows)
                    ),
                )
            record_receipt(
                store, db, marker, {"source_node_id": store.local_node_id, "source_rows": counts}
            )
        rows = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT row_json FROM access_mesh_registry_upgrade_export "
                "WHERE namespace_id=? AND table_name=? AND row_offset>=? "
                "ORDER BY row_offset LIMIT ?",
                (namespace_id, table, after, limit + 1),
            )
        ]
    more = len(rows) > limit
    rows = rows[:limit]
    if table == "access_mesh_issuer_receipts":
        for row in rows:
            _clear_receipt(store, row)
    return {
        "ok": True,
        "namespace_id": namespace_id,
        "table": table,
        "rows": rows,
        "next_offset": after + len(rows) if more else None,
        "verification_keys": store.registry_verification_keys(),
    }


def merge_page(store, *, table, rows, namespace_id, source_peer_id):
    """Idempotent union. A conflict keeps admission fenced and preserves origins."""
    if namespace_id != store.namespace_id or table not in UPGRADE_TABLES:
        raise AccessMeshError("mesh_namespace_mismatch")
    if not isinstance(rows, list) or len(rows) > 20:
        raise AccessMeshError("access_mesh_invalid_snapshot")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        store.assert_registry_ready(db)
        info = list(db.execute(f'PRAGMA table_info("{table}")'))
        columns = [r[1] for r in info]
        keys = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
        where = " AND ".join(key + "=?" for key in keys)
        native = db.execute("SELECT 1 FROM sqlite_master WHERE name='logical_agents'").fetchone()
        for original in rows:
            if not isinstance(original, dict) or set(original) != set(columns):
                raise AccessMeshError("access_mesh_invalid_snapshot")
            row = dict(original)
            existing = db.execute(
                f'SELECT * FROM "{table}" WHERE {where}', [row[k] for k in keys]
            ).fetchone()
            if existing:
                old = dict(existing)
                if table == "access_mesh_slot_replicas":
                    if any(old[k] != row[k] for k in ("logical_agent_id", "kind")):
                        raise AccessMeshError("access_mesh_registry_upgrade_conflict")
                    if old["revision"] > row["revision"]:
                        continue
                    if old["revision"] == row["revision"]:
                        # code_generation is a local cache omitted by old
                        # snapshot wire. The authoritative value is monotonic.
                        old_generation = old.pop("code_generation")
                        row_generation = row.pop("code_generation")
                        if old != row:
                            raise AccessMeshError("access_mesh_registry_upgrade_conflict")
                        row["code_generation"] = max(old_generation, row_generation)
                elif table == "access_mesh_number_claims":
                    if any(old[k] != row[k] for k in ("number", "logical_agent_id")):
                        raise AccessMeshError("access_mesh_registry_upgrade_conflict")
                    # Former issuers may have corrected a sub-millisecond
                    # persistent registration timestamp in their own replica.
                    if source_peer_id != row["issuer_id"]:
                        row["started_at"] = old["started_at"]
                    row["hard_expires_at"] = max(old["hard_expires_at"], row["hard_expires_at"])
                else:
                    if table == "access_mesh_issuer_receipts":
                        _clear_receipt(store, old)
                    if old != row:
                        raise AccessMeshError("access_mesh_registry_upgrade_conflict")
                    continue
            if table == "access_mesh_issuer_receipts":
                _seal_receipt(store, row)
            placeholders = ",".join("?" for _ in columns)
            updates = ",".join(f"{c}=excluded.{c}" for c in columns if c not in keys)
            db.execute(
                f'INSERT INTO "{table}" ({",".join(columns)}) VALUES({placeholders}) '
                f"ON CONFLICT({','.join(keys)}) DO UPDATE SET {updates}",
                [row[c] for c in columns],
            )
            if table == "access_mesh_slot_replicas" and native:
                store._native_slot(db, store._snapshot(row), store.clock().isoformat())


def complete_upgrade(store, *, member_ids=()):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        store.assert_registry_ready(db)
        # A surviving persistent registration continues to reserve its number,
        # even between sessions. Expired non-rearming history can reuse digits.
        by_number = {}
        now = store.clock()
        for row in db.execute(
            "SELECT c.number,s.* FROM access_mesh_number_claims c "
            "JOIN access_mesh_slot_replicas s USING(issuer_id,slot_id) WHERE s.state!='deleted'"
        ).fetchall():
            slot = store._snapshot(row)
            cycle = store.merged_cycle(slot, now)
            reserves = (
                slot.policy.rearm_enabled
                or not slot.anchor
                or cycle["state"] in {"active", "warning", "draining"}
            )
            if reserves:
                previous = by_number.setdefault(row["number"], slot.logical_agent_id)
                if previous != slot.logical_agent_id:
                    raise AccessMeshError("mesh_registry_number_conflict")
        defaults = db.execute(
            "SELECT policy_json,parent_json FROM access_mesh_operator_defaults WHERE singleton=1"
        ).fetchone()
        # The write lock keeps admission closed until policy propagation and
        # the completion receipt commit together with the completed union.
        db.execute("UPDATE access_mesh_registry_scope SET upgrade_pending=0 WHERE singleton=1")
        if store.registry_mesh_id and defaults and defaults["parent_json"]:
            from terminal_mcp.storage.access_policy_inheritance import _propagate

            _propagate(store, db, json.loads(defaults["policy_json"]))
        counts = {
            name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in UPGRADE_TABLES
        }
        record_receipt(
            store,
            db,
            "authoritative_registry_v1:" + store.namespace_id,
            {"member_ids": sorted(member_ids or store.trusted_issuers), "registry_rows": counts},
        )


def accept_primary_completion(store, namespace_id):
    if namespace_id != store.namespace_id:
        raise AccessMeshError("mesh_namespace_mismatch")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        store.assert_registry_ready(db)
        retired = db.execute("SELECT COUNT(*) FROM access_mesh_event_outbox").fetchone()[0]
        db.execute("DELETE FROM access_mesh_event_outbox")
        record_receipt(
            store,
            db,
            "authoritative_registry_v1:" + namespace_id,
            {
                "authority_id": store.registry_authority_id,
                "role": "replica",
                "retired_delivery_rows": retired,
            },
        )
        db.execute("UPDATE access_mesh_registry_scope SET upgrade_pending=0 WHERE singleton=1")


def migrate_receipt_kinds(store):
    """Preserve encrypted replay results while retiring the old slot label once."""
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute(
            "SELECT receipts_migrated FROM access_mesh_registry_scope WHERE singleton=1"
        ).fetchone()[0]:
            return
        changed = 0
        for original in db.execute(
            "SELECT request_key,result_json FROM access_mesh_issuer_receipts"
        ).fetchall():
            row = dict(original)
            _clear_receipt(store, row)
            result = json.loads(row["result_json"])
            if result.get("mode") != "legacy":
                continue
            result["mode"] = "temporary"
            row["result_json"] = json.dumps(result, sort_keys=True, separators=(",", ":"))
            _seal_receipt(store, row)
            db.execute(
                "UPDATE access_mesh_issuer_receipts SET result_json=? WHERE request_key=?",
                (row["result_json"], row["request_key"]),
            )
            changed += 1
        record_receipt(store, db, "temporary_receipt_kind_v1", {"changed_rows": changed})
        db.execute("UPDATE access_mesh_registry_scope SET receipts_migrated=1 WHERE singleton=1")
