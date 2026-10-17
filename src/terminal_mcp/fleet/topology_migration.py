"""One-time conversion of a former control domain to one explicit Mesh."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter

MIGRATION_TABLE = "mesh_topology_migration"


async def migrate_single_mesh(db, *, node_id: str, version: int | None, stamp: str) -> None:
    from terminal_mcp.fleet.control_storage import FleetControlError

    meta = await (
        await db.execute(
            "SELECT fleet_id,control_node_id,topology_revision,trust_revision "
            "FROM control_meta WHERE singleton=1"
        )
    ).fetchone()
    if meta is None:
        return
    memberships = await (
        await db.execute("SELECT node_id,mesh_id,state FROM managed_nodes WHERE state!='detached'")
    ).fetchall()
    scopes = {row[1] for row in memberships if row[1]}
    if len(scopes) > 1:
        # A scope cannot be picked or combined without inventing user topology.
        raise FleetControlError(
            "mesh_topology_migration_required",
            blockers=[
                {
                    "mesh_id": mesh_id,
                    "node_ids": sorted(row[0] for row in memberships if row[1] == mesh_id),
                }
                for mesh_id in sorted(scopes)
            ],
        )
    if version is None or version >= 5:
        return
    pending = await (
        await db.execute(
            "SELECT 1 FROM mesh_topology_transitions WHERE node_id=? "
            "AND phase IN ('prepared','committed')",
            (node_id,),
        )
    ).fetchone()
    mesh_id = next(iter(scopes), None)
    authority = next((row for row in memberships if row[0] == meta[1]), None)
    if mesh_id and not pending and authority is None:
        raise FleetControlError(
            "mesh_topology_migration_required",
            blockers=[
                {"mesh_id": mesh_id, "code": "mesh_primary_identity_missing"},
            ],
        )
    empty = await (
        await db.execute(
            "SELECT mesh_id FROM managed_meshes WHERE active=1 AND mesh_id NOT IN ("
            "SELECT DISTINCT mesh_id FROM managed_nodes WHERE mesh_id IS NOT NULL "
            "AND state!='detached') ORDER BY mesh_id"
        )
    ).fetchall()
    attached = bool(mesh_id and not pending and authority[1] is None)
    receipt = {
        "version": 1,
        "source_schema": version,
        "completed_at": stamp,
        "node_id": node_id,
        "primary_node_id": meta[1],
        "mesh_id": mesh_id,
        "attached_primary": attached,
        "inactive_mesh_ids": [row[0] for row in empty],
        "topology_before": meta[2],
        "trust_before": meta[3],
        "pending_local_transition": bool(pending),
    }
    # Empty declarations are retained as history and never create membership.
    for row in empty:
        await db.execute("UPDATE managed_meshes SET active=0 WHERE mesh_id=?", (row[0],))
    if attached:
        await db.execute(
            "UPDATE managed_nodes SET mesh_id=?,updated_at=? WHERE node_id=?",
            (mesh_id, stamp, meta[1]),
        )
        await db.execute(
            "UPDATE control_meta SET topology_revision=topology_revision+1,"
            "trust_revision=trust_revision+1,updated_at=? WHERE singleton=1",
            (stamp,),
        )
        await db.execute(
            "UPDATE managed_nodes SET desired_topology_revision=(SELECT topology_revision "
            "FROM control_meta WHERE singleton=1),desired_trust_revision=(SELECT trust_revision "
            "FROM control_meta WHERE singleton=1) WHERE state!='detached'"
        )
    # fleet_id is retained as an opaque transport label. Only mesh_id defines
    # the ACS scope; this conversion never invokes local registry cleanup.
    await db.execute(
        f"CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE}("
        "singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL,"
        "completed_at TEXT NOT NULL,receipt_json TEXT NOT NULL)"
    )
    await db.execute(
        f"INSERT OR IGNORE INTO {MIGRATION_TABLE} VALUES(1,1,?,?)",
        (stamp, json.dumps(receipt, sort_keys=True)),
    )


def _rows(db, table):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone():
        return []
    cursor = db.execute('SELECT * FROM "' + table.replace('"', '""') + '"')
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor]


def expected_single_mesh_rows(
    before: sqlite3.Connection,
    after: sqlite3.Connection,
    table: str,
    columns: list[str],
) -> Counter | None:
    """Independently derive the exact allowed row transformation for preservation.

    A preexisting receipt grants no exception. Memberships, revision increments
    and empty declarations are recomputed from the before database; every other
    selected column remains part of the hash.
    """
    from terminal_mcp.fleet.control_storage import FleetControlError

    if table not in {"control_meta", "managed_nodes", "managed_meshes", "control_namespace"}:
        return None
    if _rows(before, MIGRATION_TABLE):
        return None
    receipts = _rows(after, MIGRATION_TABLE)
    if not receipts:
        return None
    stored = receipts[0]
    receipt = json.loads(stored["receipt_json"])
    old_schema = _rows(before, "control_schema")
    old_meta = _rows(before, "control_meta")
    if not old_schema or not old_meta:
        return None
    meta = old_meta[0]
    members = _rows(before, "managed_nodes")
    if members and "mesh_id" not in members[0]:
        legacy = _rows(before, "managed_mesh")
        legacy_id = legacy[0]["mesh_id"] if legacy and legacy[0].get("adopted") else None
        members = [
            dict(row, mesh_id=legacy_id if row["state"] != "detached" else None) for row in members
        ]
    live = [row for row in members if row["state"] != "detached"]
    scopes = {row["mesh_id"] for row in live if row.get("mesh_id")}
    if len(scopes) > 1:
        raise FleetControlError("mesh_migration_receipt_scope_changed")
    mesh_id = next(iter(scopes), None)
    primary = next((row for row in live if row["node_id"] == meta["control_node_id"]), None)
    pending = any(
        row["node_id"] == meta["node_id"] and row["phase"] in {"prepared", "committed"}
        for row in _rows(before, "mesh_topology_transitions")
    )
    if mesh_id and not pending and primary is None:
        raise FleetControlError("mesh_migration_receipt_scope_changed")
    inactive = sorted(
        row["mesh_id"]
        for row in _rows(before, "managed_meshes")
        if row["active"] and row["mesh_id"] not in scopes
    )
    attached = bool(mesh_id and not pending and primary["mesh_id"] is None)
    expected = {
        "version": 1,
        "source_schema": old_schema[0]["version"],
        "completed_at": stored["completed_at"],
        "node_id": meta["node_id"],
        "primary_node_id": meta["control_node_id"],
        "mesh_id": mesh_id,
        "attached_primary": attached,
        "inactive_mesh_ids": inactive,
        "topology_before": meta.get("topology_revision", 0),
        "trust_before": meta.get("trust_revision", 0),
        "pending_local_transition": pending,
    }
    if (
        stored["version"] != 1
        or receipt != expected
        or old_schema[0]["version"] >= 5
        or _rows(after, "control_schema")[0]["version"] != 5
    ):
        raise FleetControlError("mesh_migration_receipt_invalid")
    result = Counter()
    for original in _rows(before, table):
        row = dict(original)
        if attached and table == "control_meta" and row.get("singleton") == 1:
            row.update(
                topology_revision=expected["topology_before"] + 1,
                trust_revision=expected["trust_before"] + 1,
                updated_at=expected["completed_at"],
            )
        elif attached and table == "managed_nodes":
            if row["node_id"] == meta["control_node_id"]:
                row.update(mesh_id=mesh_id, updated_at=expected["completed_at"])
            if row["state"] != "detached":
                row.update(
                    desired_topology_revision=expected["topology_before"] + 1,
                    desired_trust_revision=expected["trust_before"] + 1,
                )
        elif table == "managed_meshes" and row["mesh_id"] in inactive:
            row["active"] = 0
        body = json.dumps(
            [row[column] for column in columns],
            ensure_ascii=True,
            separators=(",", ":"),
            default=lambda value: {"bytes": bytes(value).hex()},
        ).encode()
        result[hashlib.sha256(body).hexdigest()] += 1
    return result
