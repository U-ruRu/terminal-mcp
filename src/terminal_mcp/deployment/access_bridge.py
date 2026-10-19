"""Offline preparation of the deployed Windows v25 Access catalog for v26 -> v27."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from terminal_mcp.deployment._release_common import require


def source_revision(path: Path) -> int:
    if not path.exists():
        return 0
    with closing(sqlite3.connect(path)) as db:
        return int(db.execute("PRAGMA user_version").fetchone()[0])


def prepare_access_v26(path: Path, *, source_version: int):
    if source_version != 25:
        return
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        require(
            db.execute("PRAGMA user_version").fetchone()[0] == 27, "repository_preparation_required"
        )
        definition = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='access_mesh_slot_replicas'"
        ).fetchone()
        if definition is None:
            return
        schema = definition[0]
        columns = [row[1] for row in db.execute("PRAGMA table_info(access_mesh_slot_replicas)")]
        require(
            "logical_agent_id" in columns and "'legacy'" in schema, "access_v25_schema_unrecognized"
        )
        require(
            not db.execute(
                "SELECT 1 FROM access_mesh_slot_replicas WHERE state='deleted' LIMIT 1"
            ).fetchone(),
            "access_v25_deleted_registration_requires_review",
        )
        objects = [
            row[0]
            for row in db.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name='access_mesh_slot_replicas' "
                "AND type IN ('index','trigger') AND sql IS NOT NULL"
            )
        ]
        upgraded = re.sub(r"\baccess_mesh_slot_replicas\b", "access_bridge_v26", schema, count=1)
        db.execute(upgraded.replace("'legacy'", "'temporary'"))
        quoted = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
        selected = ",".join(
            "CASE kind WHEN 'legacy' THEN 'temporary' ELSE kind END"
            if name == "kind"
            else '"' + name.replace('"', '""') + '"'
            for name in columns
        )
        db.execute(
            f"INSERT INTO access_bridge_v26({quoted}) SELECT {selected} "
            "FROM access_mesh_slot_replicas"
        )
        db.execute("DROP TABLE access_mesh_slot_replicas")
        db.execute("ALTER TABLE access_bridge_v26 RENAME TO access_mesh_slot_replicas")
        for sql in objects:
            db.execute(sql)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "access_mesh_event_outbox" in tables:
            for peer, event, raw in db.execute(
                "SELECT peer_node_id,event_id,payload_json FROM access_mesh_event_outbox"
            ).fetchall():
                payload = json.loads(raw)
                if payload.get("issued_kind") == "legacy":
                    payload["issued_kind"] = "temporary"
                    db.execute(
                        "UPDATE access_mesh_event_outbox SET payload_json=? "
                        "WHERE peer_node_id=? AND event_id=?",
                        (json.dumps(payload, sort_keys=True), peer, event),
                    )
        db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration_state"
            "(name TEXT PRIMARY KEY,value INTEGER NOT NULL) WITHOUT ROWID"
        )
        # Repository preparation has completed. The remaining Access transition starts at v26.
        db.execute(
            "INSERT INTO schema_migration_state VALUES('repository_schema_from',26) "
            "ON CONFLICT(name) DO UPDATE SET value=26"
        )
        require(
            db.execute("PRAGMA foreign_key_check").fetchone() is None,
            "access_v25_foreign_keys_changed",
        )
        db.commit()
