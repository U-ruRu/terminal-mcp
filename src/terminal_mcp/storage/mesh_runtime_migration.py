"""One-time retirement of the pre-ACS agent runtime.

The migration keeps application rows and identity credentials. Native mappings
are committed with retirement of old session permissions; a receipt prevents
old registrations from being imported again after a node changes Mesh.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from terminal_mcp.core.access_mesh_grants import AccessSlotEvent, SlotPolicy
from terminal_mcp.core.human_names import number_agent_name

MIGRATION_TABLE = "unified_runtime_migration"
MIGRATION_VERSION = 1
MIGRATION_REASON = "unified_mesh_upgrade"


class RuntimeMigrationError(RuntimeError):
    pass


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise RuntimeMigrationError("legacy_session_time_invalid")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        _stamp(parsed)
        return parsed
    except (TypeError, ValueError) as exc:
        raise RuntimeMigrationError("legacy_session_time_invalid") from exc


def _receipt(db):
    if MIGRATION_TABLE not in _tables(db):
        return None
    row = db.execute(
        f"SELECT version,completed_at,receipt_json FROM {MIGRATION_TABLE} WHERE singleton=1"
    ).fetchone()
    if row is None:
        return None
    if row[0] != MIGRATION_VERSION:
        raise RuntimeMigrationError("unified_runtime_migration_version_unsupported")
    receipt = json.loads(row[2])
    if receipt.get("version") != row[0] or receipt.get("completed_at") != row[1]:
        raise RuntimeMigrationError("unified_runtime_migration_receipt_invalid")
    _parse(row[1])
    return receipt


def prior_persistent_defaults(settings) -> SlotPolicy | None:
    """Capture the old local parent policy only before first native registration."""
    if not settings.database_path.exists():
        return None
    with sqlite3.connect(f"{settings.database_path.resolve().as_uri()}?mode=ro", uri=True) as db:
        tables = _tables(db)
        if "access_mesh_slot_replicas" in tables or "logical_agents" not in tables:
            return None
        if not db.execute("SELECT 1 FROM logical_agents LIMIT 1").fetchone():
            return None
    duration = settings.persistent_session_duration_sec
    return SlotPolicy(
        duration,
        settings.persistent_session_rearm_after_sec,
        True,
        duration - settings.persistent_session_warning_after_sec,
        duration - settings.persistent_session_alert_after_sec,
    )


def assert_runtime_migration_safe(path: Path) -> None:
    """Refuse unmapped live execution before generic startup recovery touches it."""
    if not Path(path).exists():
        return
    db = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        if _receipt(db) is not None:
            return
        tables = _tables(db)
        native = "access_mesh_native_owners" in tables
        native_filter = (
            "NOT EXISTS (SELECT 1 FROM access_mesh_native_owners n "
            "WHERE n.logical_agent_id=COALESCE(a.logical_agent_id,a.agent_id))"
            if native
            else "1"
        )
        if {"commands", "command_agent_attribution"} <= tables:
            columns = {r[1] for r in db.execute("PRAGMA table_info(command_agent_attribution)")}
            if "logical_agent_id" not in columns:
                native_filter = native_filter.replace(
                    "COALESCE(a.logical_agent_id,a.agent_id)", "a.agent_id"
                )
            active = db.execute(
                "SELECT 1 FROM commands c JOIN command_agent_attribution a "
                "ON c.hash=a.command_hash WHERE c.status IN ('queued','running','cancelling') "
                "AND a.agent_id!='anonymous' AND " + native_filter + " LIMIT 1"
            ).fetchone()
            if active:
                raise RuntimeMigrationError("legacy_active_work_requires_drain")
        if "logical_agent_work_sessions" in tables:
            native_filter = (
                "AND NOT EXISTS(SELECT 1 FROM access_mesh_native_owners n "
                "WHERE n.logical_agent_id=s.logical_agent_id)"
                if native
                else ""
            )
            if db.execute(
                "SELECT 1 FROM logical_agent_work_sessions s WHERE state='stopping' "
                + native_filter
                + " LIMIT 1"
            ).fetchone():
                raise RuntimeMigrationError("legacy_session_transition_pending")
    finally:
        db.close()


def _policy(db, agent_id, default):
    if "logical_agent_session_policies" not in _tables(db):
        return default, True
    row = db.execute(
        "SELECT policy_json FROM logical_agent_session_policies WHERE logical_agent_id=?",
        (agent_id,),
    ).fetchone()
    if row is None:
        return default, True
    values = json.loads(row[0])
    duration = int(values["default_duration_seconds"])
    return SlotPolicy(
        duration,
        int(values["rearm_after_seconds"]),
        True,
        min(int(values["warning_before_expiry_seconds"]), duration - 1),
        min(int(values["draining_before_expiry_seconds"]), duration - 1),
        default.release_on_end,
    ), False


def _allocate(db, store, agent_id, stamp):
    first = int(hashlib.sha256(agent_id.encode()).hexdigest()[:8], 16) % 10000
    attempt = "runtime-upgrade-" + hashlib.sha256(agent_id.encode()).hexdigest()[:32]
    for offset in range(10000):
        number = f"{(first + offset) % 10000:04d}"
        if not store.numbers._occupied(db, number, attempt, as_of=stamp):
            return number, attempt
    raise RuntimeMigrationError("session_number_capacity")


def _import_slot(db, store, agent, session, *, policy, inherited, kind, now):
    agent_id = agent["logical_agent_id"]
    slot_id = "upgrade-" + hashlib.sha256(agent_id.encode()).hexdigest()[:32]
    number, attempt = _allocate(db, store, agent_id, _stamp(now))
    started = _parse(session["started_at"]) if session else now
    deadline = _parse(session["hard_expires_at"]) if session else started
    if deadline < started:
        raise RuntimeMigrationError("legacy_session_time_invalid")
    db.execute(
        "INSERT INTO access_mesh_native_owners VALUES(?,?,?)",
        (agent_id, store.local_node_id, slot_id),
    )
    event = AccessSlotEvent(
        store.local_node_id,
        slot_id,
        agent_id,
        1,
        "runtime-upgrade-" + slot_id,
        "SlotIssued",
        policy=policy,
        code_tag=store.code_tag(store.local_node_id, number),
        effective_at=started if session else None,
        agent_name=number_agent_name(number),
        display_suffix=agent["display_name"][:80] or None,
        inherit_policy=inherited,
    )
    store.import_runtime_event(
        event,
        issued_kind=kind,
        mutation_number={"number": number, "attempt_id": attempt, "started_at": started},
        _connection=db,
    )
    # The historical local cycle keeps its exact deadline, including fractional
    # seconds. No implicit new window is granted to an inactive registration.
    db.execute(
        "UPDATE access_mesh_number_claims SET hard_expires_at=? WHERE issuer_id=? AND slot_id=?",
        (_stamp(deadline), store.local_node_id, slot_id),
    )
    if session:
        if deadline > started:
            store.import_runtime_event(
                AccessSlotEvent(
                    store.local_node_id,
                    slot_id,
                    agent_id,
                    2,
                    "runtime-upgrade-session-" + slot_id,
                    "SessionUpdated",
                    effective_at=started,
                    deadline_at=deadline,
                ),
                _connection=db,
            )
        db.execute(
            "INSERT INTO access_mesh_local_sessions "
            "(issuer_id,slot_id,work_session_id,cycle_started_at,session_epoch) VALUES(?,?,?,?,?)",
            (
                store.local_node_id,
                slot_id,
                session["work_session_id"],
                _stamp(started),
                session["session_epoch"],
            ),
        )
    if agent["state"] == "suspended":
        revision = 3 if session and deadline > started else 2
        store.import_runtime_event(
            AccessSlotEvent(
                store.local_node_id,
                slot_id,
                agent_id,
                revision,
                "runtime-upgrade-suspend-" + slot_id,
                "SlotSuspended",
            ),
            _connection=db,
        )
        # _native_slot normally updates the local projection. Existing source
        # data is preserved byte-for-byte; only the new native catalog changes.
        db.execute(
            "UPDATE logical_agents SET state=?,updated_at=?,deleted_at=? WHERE logical_agent_id=?",
            (agent["state"], agent["updated_at"], agent["deleted_at"], agent_id),
        )
    return {
        "logical_agent_id": agent_id,
        "issuer_id": store.local_node_id,
        "slot_id": slot_id,
        "work_session_id": session["work_session_id"] if session else None,
        "session_epoch": session["session_epoch"] if session else None,
        "started_at": session["started_at"] if session else None,
        "hard_expires_at": session["hard_expires_at"] if session else None,
    }


def migrate_unified_runtime(
    store,
    *,
    defaults: SlotPolicy,
    existing_installation: bool,
    preserve_local_defaults: bool = False,
    now: datetime | None = None,
) -> dict:
    """Call after repository schema migration and before topology reconciliation."""
    assert_runtime_migration_safe(store.path)
    now = now or datetime.now(UTC)
    stamp = _stamp(now)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = _receipt(db)
        if previous is not None:
            return previous
        db.execute(f"""CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL,
            completed_at TEXT NOT NULL, receipt_json TEXT NOT NULL)""")
        if preserve_local_defaults:
            from terminal_mcp.storage.access_policy_inheritance import _write

            _write(
                db,
                revision=2,
                policy=asdict(defaults),
                inherit_policy=False,
                parent={"source": "server", "revision": 1, "policy": asdict(defaults)},
            )
        if existing_installation:
            columns = {r[1] for r in db.execute("PRAGMA table_info(access_mesh_registry_scope)")}
            if "upgrade_pending" in columns:
                db.execute(
                    "UPDATE access_mesh_registry_scope SET upgrade_pending=1 WHERE singleton=1"
                )
        imported = []
        sessions = db.execute(
            "SELECT * FROM agent_sessions WHERE state='active' ORDER BY agent_id"
        ).fetchall()
        local_sessions = [
            s for s in sessions if s["source_instance_id"] in {None, "", store.local_node_id}
        ]
        local_ids = {s["agent_id"] for s in local_sessions}
        legacy_claim_ids = []
        for legacy in local_sessions:
            agent_id = legacy["agent_id"]
            if db.execute(
                "SELECT 1 FROM logical_agents WHERE logical_agent_id=?", (agent_id,)
            ).fetchone():
                raise RuntimeMigrationError("legacy_agent_identity_conflict")
            _parse(legacy["registered_at"])
            if not legacy["global_expires_at"]:
                raise RuntimeMigrationError("legacy_session_deadline_missing")
            _parse(legacy["global_expires_at"])
            db.execute(
                "INSERT INTO logical_agents(logical_agent_id,display_name,state,authority_node_id,"
                "authority_epoch,slot_revision,selector_generation,auth_generation,"
                "created_at,updated_at) "
                "VALUES(?,?,'active',?,1,1,1,1,?,?)",
                (
                    agent_id,
                    legacy["task_summary"][:80],
                    store.local_node_id,
                    legacy["registered_at"],
                    legacy["last_activity_at"],
                ),
            )
            session_id = "wm_upgrade_" + hashlib.sha256(agent_id.encode()).hexdigest()[:32]
            db.execute(
                "INSERT INTO logical_agent_work_sessions(work_session_id,logical_agent_id,"
                "session_epoch,"
                "authority_node_id,authority_epoch,started_at,hard_expires_at,auth_generation,"
                "state,origin_instance_id) "
                "VALUES(?,?,1,?,1,?,?,1,'active',?)",
                (
                    session_id,
                    agent_id,
                    store.local_node_id,
                    legacy["registered_at"],
                    legacy["global_expires_at"],
                    store.local_node_id,
                ),
            )
            legacy_claim_ids.append(agent_id)
        rows = db.execute(
            "SELECT * FROM logical_agents a WHERE authority_node_id=? "
            "AND state NOT IN ('deleted','deleting') "
            "AND NOT EXISTS(SELECT 1 FROM access_mesh_native_owners n "
            "WHERE n.logical_agent_id=a.logical_agent_id) "
            "ORDER BY logical_agent_id",
            (store.local_node_id,),
        ).fetchall()
        for agent in rows:
            active = db.execute(
                "SELECT * FROM logical_agent_work_sessions WHERE logical_agent_id=? "
                "AND state='active' ORDER BY session_epoch DESC",
                (agent["logical_agent_id"],),
            ).fetchall()
            if len(active) > 1:
                raise RuntimeMigrationError("legacy_session_identity_ambiguous")
            if active and agent["state"] == "suspended":
                raise RuntimeMigrationError("legacy_session_state_inconsistent")
            policy, inherited = _policy(db, agent["logical_agent_id"], defaults)
            temporary = agent["logical_agent_id"] in local_ids
            if temporary:
                policy = replace(policy, rearm_enabled=False, release_on_end=True)
            imported.append(
                _import_slot(
                    db,
                    store,
                    agent,
                    active[0] if active else None,
                    policy=policy,
                    inherited=inherited,
                    kind="temporary" if temporary else "persistent",
                    now=now,
                )
            )
        for agent_id in legacy_claim_ids:
            db.execute(
                "UPDATE work_claims SET owner_kind='logical_agent' "
                "WHERE owner_kind='legacy_session' "
                "AND owner_id=? AND released_at IS NULL",
                (agent_id,),
            )
        db.execute(
            "UPDATE agent_sessions SET state='forced',ended_at=COALESCE(ended_at,?),"
            "end_reason=? WHERE state='active'",
            (stamp, MIGRATION_REASON),
        )
        receipt = {
            "version": MIGRATION_VERSION,
            "completed_at": stamp,
            "namespace_id": store.namespace_id,
            "node_id": store.local_node_id,
            "retired_session_ids": [s["agent_id"] for s in sessions],
            "converted_claim_owner_ids": legacy_claim_ids,
            "imported": imported,
            "preserved_local_defaults": asdict(defaults) if preserve_local_defaults else None,
        }
        db.execute(
            f"INSERT INTO {MIGRATION_TABLE} VALUES(1,?,?,?)",
            (MIGRATION_VERSION, stamp, json.dumps(receipt, sort_keys=True)),
        )
        return receipt


def expected_unified_runtime_rows(
    before: sqlite3.Connection, after: sqlite3.Connection, table: str, columns: list[str]
) -> Counter | None:
    """Return narrowly normalized old row hashes for the deployment preserve gate.

    A prior receipt grants no exception. Mappings and exact old session deadlines
    are independently checked in the resulting database before any normalization.
    Unrelated columns remain part of the row hash and cannot be silently changed.
    """
    if table not in {"agent_sessions", "work_claims"} or _receipt(before) is not None:
        return None
    receipt = _receipt(after)
    if receipt is None:
        return None
    old_sessions = {
        row[0]: row
        for row in before.execute(
            "SELECT agent_id,registered_at,global_expires_at,source_instance_id "
            "FROM agent_sessions WHERE state='active'"
        )
    }
    local_ids = {
        agent_id
        for agent_id, row in old_sessions.items()
        if row[3] in {None, "", receipt["node_id"]}
    }
    if (
        set(receipt["retired_session_ids"]) != set(old_sessions)
        or set(receipt["converted_claim_owner_ids"]) != local_ids
    ):
        raise RuntimeMigrationError("runtime_migration_receipt_scope_changed")
    mappings = {item["logical_agent_id"]: item for item in receipt["imported"]}
    for agent_id in receipt["converted_claim_owner_ids"]:
        item = mappings.get(agent_id)
        if item is None:
            raise RuntimeMigrationError("runtime_migration_mapping_missing")
        old = old_sessions[agent_id]
        if (
            item["issuer_id"] != receipt["node_id"]
            or item["session_epoch"] != 1
            or (item["started_at"], item["hard_expires_at"]) != (old[1], old[2])
        ):
            raise RuntimeMigrationError("runtime_migration_mapping_changed")
        slot = after.execute(
            "SELECT logical_agent_id,kind FROM access_mesh_slot_replicas "
            "WHERE issuer_id=? AND slot_id=?",
            (item["issuer_id"], item["slot_id"]),
        ).fetchone()
        session = after.execute(
            "SELECT logical_agent_id,session_epoch,started_at,hard_expires_at "
            "FROM logical_agent_work_sessions "
            "WHERE work_session_id=?",
            (item["work_session_id"],),
        ).fetchone()
        local = after.execute(
            "SELECT work_session_id,session_epoch FROM access_mesh_local_sessions "
            "WHERE issuer_id=? AND slot_id=?",
            (item["issuer_id"], item["slot_id"]),
        ).fetchone()
        if (
            slot is None
            or tuple(slot) != (agent_id, "temporary")
            or session is None
            or local is None
        ):
            raise RuntimeMigrationError("runtime_migration_mapping_missing")
        if tuple(session) != (
            agent_id,
            item["session_epoch"],
            item["started_at"],
            item["hard_expires_at"],
        ) or tuple(local) != (item["work_session_id"], item["session_epoch"]):
            raise RuntimeMigrationError("runtime_migration_mapping_changed")

    def quote(value):
        return '"' + value.replace('"', '""') + '"'

    result = Counter()
    retired = set(receipt["retired_session_ids"])
    converted = set(receipt["converted_claim_owner_ids"])
    for values in before.execute(
        "SELECT " + ",".join(map(quote, columns)) + " FROM " + quote(table)
    ):
        row = dict(zip(columns, values, strict=True))
        if (
            table == "agent_sessions"
            and row.get("agent_id") in retired
            and row["state"] == "active"
        ):
            row.update(
                state="forced",
                ended_at=row.get("ended_at") or receipt["completed_at"],
                end_reason=MIGRATION_REASON,
            )
        elif (
            table == "work_claims"
            and row.get("owner_kind") == "legacy_session"
            and row.get("owner_id") in converted
            and row.get("released_at") is None
        ):
            row["owner_kind"] = "logical_agent"
        body = json.dumps(
            [row[c] for c in columns],
            ensure_ascii=True,
            separators=(",", ":"),
            default=lambda value: {"bytes": bytes(value).hex()},
        ).encode()
        result[hashlib.sha256(body).hexdigest()] += 1
    return result
