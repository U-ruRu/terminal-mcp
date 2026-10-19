"""Narrow, independently checked row conversions for the unified Mesh upgrade.

The deployment gate remains a multiset comparison over every old application
column. These adapters describe known schema changes; no table-wide data-loss
exemption or trusted list of arbitrary row hashes is accepted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from terminal_mcp.deployment._release_common import ReleaseError, require

RETIRED_COLUMNS = {
    "access_mesh_operator_defaults": {"legacy_enabled"},
    "access_policy": {"legacy_admission_enabled"},
    "access_mesh_slot_replicas": {"logical_agent_id"},
    "access_mesh_number_claims": {"logical_agent_id"},
}


def _session_split_rows(before, after):
    """Verify identity relocation and derive only the old closed-window fields."""
    columns = [row[1] for row in before.execute("PRAGMA table_info(access_mesh_slot_replicas)")]
    if "logical_agent_id" not in columns:
        return {}
    require(
        before.execute("PRAGMA user_version").fetchone()[0] in {25, 26}
        and after.execute("PRAGMA user_version").fetchone()[0] == 27,
        "unsupported_access_session_migration",
    )

    def parsed(value):
        if value is None:
            return None
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(stamp.tzinfo is not None, "access_session_migration_time_invalid")
        return stamp.astimezone(UTC)

    result = {}
    for values in before.execute("SELECT * FROM access_mesh_slot_replicas"):
        row = dict(zip(columns, values, strict=True))
        require(row["state"] != "deleted", "access_deleted_registration_requires_review")
        key = row["issuer_id"], row["slot_id"]
        history = [
            json.loads(event[0])
            for event in before.execute(
                "SELECT event_json FROM access_mesh_event_log WHERE issuer_id=? AND slot_id=? "
                "ORDER BY revision",
                key,
            )
        ]
        windows = [
            event
            for event in history
            if event["kind"] in {"SlotIssued", "SessionStarted", "SessionUpdated"}
            and event.get("effective_at")
        ]
        endings = [event for event in history if event["kind"] == "SessionEnded"]
        window, ending = (windows[-1] if windows else {}), (endings[-1] if endings else {})
        start = parsed(window.get("effective_at") if window else row["anchor"])
        end = parsed(ending.get("effective_at"))
        closed = bool(ending and (not window or ending["revision"] > window["revision"]))
        if end is not None and start is not None and start > end:
            end = None
        active_from = parsed(window.get("active_from")) or start
        actual = after.execute(
            "SELECT logical_agent_id,active,ended_at,active_from FROM access_mesh_sessions "
            "WHERE issuer_id=? AND slot_id=?",
            key,
        ).fetchone()
        expected = (
            row["logical_agent_id"],
            int(not closed),
            end.isoformat() if end else None,
            active_from.isoformat() if active_from else None,
        )
        require(
            actual is not None and tuple(actual) == expected,
            "access_session_identity_or_window_changed",
        )
        if closed and start is not None:
            captured = json.loads(row["policy_window_json"]) if row["policy_window_json"] else {}
            row["anchor"] = start.isoformat()
            row["deadline_at"] = (
                window.get("deadline_at")
                or captured.get("hard_expires_at")
                or (
                    start + timedelta(seconds=json.loads(row["policy_json"])["duration_seconds"])
                ).isoformat()
            )
        result[key] = row
    return result


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _complete(db):
    if "unified_runtime_migration" not in _tables(db):
        return False
    row = db.execute(
        "SELECT version,completed_at,receipt_json FROM unified_runtime_migration WHERE singleton=1"
    ).fetchone()
    if row is None:
        return False
    receipt = json.loads(row[2])
    require(row[0] == receipt.get("version") == 1, "unsupported_runtime_migration_receipt")
    require(row[1] == receipt.get("completed_at"), "runtime_migration_receipt_invalid")
    return True


def receipt_cipher(runtime_path: Path) -> AESGCM | None:
    """Read immutable installation identity without constructing or changing stores."""
    path = runtime_path.with_name("node-identity.json")
    if not path.exists():
        return None
    require(path.is_file() and path.stat().st_size <= 16384, "invalid_node_identity_file")
    try:
        identity = json.loads(path.read_text())
        node_id, proof = identity["node_id"], identity["access_proof_key"]
        require(
            isinstance(node_id, str)
            and bool(node_id)
            and isinstance(proof, str)
            and len(proof.encode()) >= 32,
            "invalid_node_identity_file",
        )
        key = hmac.new(
            proof.encode(), f"access-mesh-issuer-receipt-v2:{node_id}".encode(), hashlib.sha256
        ).digest()
    except (ValueError, KeyError, TypeError) as exc:
        raise ReleaseError("invalid_node_identity_file") from exc
    return AESGCM(key)


def comparable_columns(before, after, table: str, columns: list[str]) -> list[str]:
    quoted = table.replace('"', '""')
    current = {r[1] for r in after.execute(f'PRAGMA table_info("{quoted}")')}
    missing = set(columns) - current
    require(missing <= RETIRED_COLUMNS.get(table, set()), "protected_state_columns_removed")
    if missing:
        if table == "access_policy":
            from terminal_mcp.fleet.control_storage import FleetControlStore

            version = after.execute(
                "SELECT version FROM control_schema WHERE singleton=1"
            ).fetchone()
            require(
                version and version[0] == FleetControlStore.SCHEMA_VERSION,
                "unsupported_control_schema_migration",
            )
        elif table == "access_mesh_slot_replicas":
            _session_split_rows(before, after)
        elif table == "access_mesh_number_claims":
            _session_split_rows(before, after)
            for issuer, slot, agent in before.execute(
                "SELECT issuer_id,slot_id,logical_agent_id FROM access_mesh_number_claims"
            ):
                session = after.execute(
                    "SELECT logical_agent_id FROM access_mesh_sessions "
                    "WHERE issuer_id=? AND slot_id=?",
                    (issuer, slot),
                ).fetchone()
                require(
                    session is not None and session[0] == agent, "access_number_identity_changed"
                )
        else:
            schema = after.execute(
                "SELECT sql FROM sqlite_master WHERE name='access_mesh_slot_replicas'"
            ).fetchone()
            require(schema and "'temporary'" in schema[0], "native_slot_migration_required")
    return [column for column in columns if column in current]


def _hashes(db, table, columns, convert):
    def quote(value):
        return '"' + value.replace('"', '""') + '"'

    result = Counter()
    for values in db.execute("SELECT " + ",".join(map(quote, columns)) + " FROM " + quote(table)):
        row = dict(zip(columns, values, strict=True))
        if not convert(row):
            continue
        body = json.dumps(
            [row[c] for c in columns],
            ensure_ascii=True,
            separators=(",", ":"),
            default=lambda value: {"bytes": bytes(value).hex()},
        ).encode()
        result[hashlib.sha256(body).hexdigest()] += 1
    return result


def schema_row_counters(before, after, table, columns, *, cipher=None):
    supported = {
        "access_mesh_slot_replicas",
        "access_mesh_event_outbox",
        "access_mesh_issuer_receipts",
        "access_mesh_registry_scope",
        "control_schema",
        "schema_migration_state",
    }
    if table not in supported:
        return None
    first_runtime_upgrade = _complete(after) and not _complete(before)
    split = _session_split_rows(before, after) if table == "access_mesh_slot_replicas" else {}

    def convert(row, *, original):
        if table == "access_mesh_slot_replicas" and original and split:
            mapped = split[(row["issuer_id"], row["slot_id"])]
            for field in ("anchor", "deadline_at"):
                if field in row:
                    row[field] = mapped[field]
        if table == "access_mesh_slot_replicas" and original and row.get("kind") == "legacy":
            row["kind"] = "temporary"
        elif table == "access_mesh_event_outbox":
            payload = json.loads(row["payload_json"])
            if original and payload.get("issued_kind") == "legacy":
                payload["issued_kind"] = "temporary"
            row["payload_json"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        elif table == "access_mesh_issuer_receipts":
            require(cipher is not None, "protected_receipt_key_unavailable")
            try:
                sealed = base64.b64decode(row["result_json"], validate=True)
                clear = cipher.decrypt(sealed[:12], sealed[12:], row["request_key"].encode())
                payload = json.loads(clear)
            except Exception as exc:
                raise ReleaseError("protected_receipt_unreadable") from exc
            if original and payload.get("mode") == "legacy":
                payload["mode"] = "temporary"
            row["result_json"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        elif table == "access_mesh_registry_scope" and original:
            if first_runtime_upgrade and "upgrade_pending" in row:
                row["upgrade_pending"] = 1
            if "receipts_migrated" in row:
                row["receipts_migrated"] = 1
        elif table == "control_schema" and original and row.get("version") in {1, 2, 3, 4, 5}:
            from terminal_mcp.fleet.control_storage import FleetControlStore

            row["version"] = FleetControlStore.SCHEMA_VERSION
        elif table == "schema_migration_state" and first_runtime_upgrade:
            if row.get("name") in {"repository_schema_from", "access_defaults_snapshot_required"}:
                return False
        return True

    return (
        _hashes(before, table, columns, lambda row: convert(row, original=True)),
        _hashes(after, table, columns, lambda row: convert(row, original=False)),
    )
