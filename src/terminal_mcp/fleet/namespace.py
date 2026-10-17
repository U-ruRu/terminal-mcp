"""Finish a committed, authorized namespace change without rotating identity keys.

Control state commits the intent with the accepted membership snapshot. Configuration
and separate metadata files are repaired idempotently before normal startup/reconcile.
Neither a namespace string nor this local journal authenticates a remote caller.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.core.persistent_policy import PersistentPolicyController
from terminal_mcp.fleet.projection_storage import PROJECTION_CACHE_TABLES
from terminal_mcp.fleet.protocol import validate_protocol_id


def finish_namespace_transition(settings) -> str | None:
    control = Path(settings.effective_fleet_control_path())
    if not control.is_file():
        return None
    with closing(sqlite3.connect(control)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='control_namespace'").fetchone():
            return None
        row = db.execute(
            "SELECT n.pending_fleet_id,m.fleet_id,m.node_id,n.updated_at FROM control_namespace n "
            "JOIN control_meta m USING(singleton) WHERE n.pending_fleet_id IS NOT NULL"
        ).fetchone()
        operation = None
        if (
            row is not None
            and db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='mesh_topology_transitions'"
            ).fetchone()
        ):
            operation = db.execute(
                "SELECT operation_id FROM mesh_topology_transitions WHERE node_id=?",
                (row[2],),
            ).fetchone()
    if row is None:
        return None
    namespace = validate_protocol_id(str(row[0]), "fleet_id")
    node_id = settings.effective_fleet_node_id()
    if row[1] != namespace or row[2] != node_id:
        raise RuntimeError("namespace_transition_identity_mismatch")
    transition_id = operation[0] if operation else row[3]
    for path, table in (
        (settings.effective_fleet_node_meta_path(), "source_meta"),
        (settings.effective_fleet_projection_path(), "projection_meta"),
    ):
        path = Path(path)
        if not path.is_file():
            continue
        with closing(sqlite3.connect(path)) as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                continue
            identity = db.execute(f"SELECT node_id FROM {table} WHERE singleton=1").fetchone()
            if identity is not None and identity[0] != node_id:
                raise RuntimeError("namespace_transition_identity_mismatch")
            if table == "projection_meta":
                db.execute(
                    "CREATE TABLE IF NOT EXISTS projection_namespace_applied("
                    "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                    "namespace_id TEXT NOT NULL,transition_id TEXT NOT NULL)"
                )
                applied = db.execute(
                    "SELECT namespace_id,transition_id FROM projection_namespace_applied "
                    "WHERE singleton=1"
                ).fetchone()
                if applied != (namespace, transition_id):
                    # This database contains only derived read models. Delete
                    # old Mesh rows atomically with the new namespace marker;
                    # source generations and main application history survive.
                    for cache in PROJECTION_CACHE_TABLES:
                        if db.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (cache,)
                        ).fetchone():
                            db.execute(f'DELETE FROM "{cache}"')
                    db.execute(
                        "UPDATE projection_meta SET fleet_id=?,projection_epoch=projection_epoch+1,"
                        "projection_seq=0,updated_at=? WHERE singleton=1",
                        (namespace, utc_text()),
                    )
                    db.execute(
                        "INSERT INTO projection_namespace_applied VALUES(1,?,?) "
                        "ON CONFLICT(singleton) DO UPDATE SET namespace_id=excluded.namespace_id,"
                        "transition_id=excluded.transition_id",
                        (namespace, transition_id),
                    )
            else:
                db.execute(f"UPDATE {table} SET fleet_id=? WHERE singleton=1", (namespace,))
            db.commit()
    # The existing config writer preserves other settings, file mode and secrets.
    PersistentPolicyController._persist_env_updates(
        Path(settings.env_file_path), {"TERMINAL_MCP_FLEET_ID": namespace}
    )
    settings.fleet_id = namespace
    with closing(sqlite3.connect(control)) as db:
        db.execute(
            "UPDATE control_namespace SET pending_fleet_id=NULL "
            "WHERE singleton=1 AND pending_fleet_id=?",
            (namespace,),
        )
        db.commit()
    return namespace
