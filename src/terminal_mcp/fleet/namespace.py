"""Finish a committed, authorized namespace change without rotating identity keys.

Control state commits the intent with the accepted membership snapshot. Configuration
and separate metadata files are repaired idempotently before normal startup/reconcile.
Neither a namespace string nor this local journal authenticates a remote caller.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from terminal_mcp.core.persistent_policy import PersistentPolicyController
from terminal_mcp.fleet.protocol import validate_protocol_id


def finish_namespace_transition(settings) -> str | None:
    control = Path(settings.effective_fleet_control_path())
    if not control.is_file():
        return None
    with closing(sqlite3.connect(control)) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='control_namespace'"
        ).fetchone():
            return None
        row = db.execute(
            "SELECT n.pending_fleet_id,m.fleet_id,m.node_id FROM control_namespace n "
            "JOIN control_meta m USING(singleton) WHERE n.pending_fleet_id IS NOT NULL"
        ).fetchone()
    if row is None:
        return None
    namespace = validate_protocol_id(str(row[0]), "fleet_id")
    node_id = settings.effective_fleet_node_id()
    if row[1] != namespace or row[2] != node_id:
        raise RuntimeError("namespace_transition_identity_mismatch")
    for path, table in (
        (settings.effective_fleet_node_meta_path(), "source_meta"),
        (settings.effective_fleet_projection_path(), "projection_meta"),
    ):
        path = Path(path)
        if not path.is_file():
            continue
        with closing(sqlite3.connect(path)) as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (table,)
            ).fetchone():
                continue
            identity = db.execute(f"SELECT node_id FROM {table} WHERE singleton=1").fetchone()
            if identity is not None and identity[0] != node_id:
                raise RuntimeError("namespace_transition_identity_mismatch")
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
            "WHERE singleton=1 AND pending_fleet_id=?", (namespace,)
        )
        db.commit()
    return namespace
