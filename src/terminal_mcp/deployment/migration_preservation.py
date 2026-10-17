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
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from terminal_mcp.deployment._release_common import ReleaseError, require

RETIRED_COLUMNS = {
    "access_mesh_operator_defaults": {"legacy_enabled"},
    "access_policy": {"legacy_admission_enabled"},
}


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
            version = after.execute(
                "SELECT version FROM control_schema WHERE singleton=1"
            ).fetchone()
            require(version and version[0] == 5, "unsupported_control_schema_migration")
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
            [row[c] for c in columns], ensure_ascii=True, separators=(",", ":"),
            default=lambda value: {"bytes": bytes(value).hex()},
        ).encode()
        result[hashlib.sha256(body).hexdigest()] += 1
    return result


def schema_row_counters(before, after, table, columns, *, cipher=None):
    supported = {
        "access_mesh_slot_replicas", "access_mesh_event_outbox",
        "access_mesh_issuer_receipts", "access_mesh_registry_scope",
        "control_schema", "schema_migration_state",
    }
    if table not in supported:
        return None
    first_runtime_upgrade = _complete(after) and not _complete(before)

    def convert(row, *, original):
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
        elif table == "control_schema" and original and row.get("version") in {1, 2, 3, 4}:
            row["version"] = 5
        elif table == "schema_migration_state" and first_runtime_upgrade:
            if row.get("name") in {"repository_schema_from", "access_defaults_snapshot_required"}:
                return False
        return True

    return (
        _hashes(before, table, columns, lambda row: convert(row, original=True)),
        _hashes(after, table, columns, lambda row: convert(row, original=False)),
    )
