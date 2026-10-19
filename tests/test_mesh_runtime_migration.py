"""Atomic old-runtime retirement with verifiable preservation and no reimport."""

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from terminal_mcp.core.access_mesh_grants import SlotPolicy
from terminal_mcp.storage.access_mesh import AccessMeshStore
from terminal_mcp.storage.mesh_runtime_migration import (
    MIGRATION_REASON,
    MIGRATION_TABLE,
    RuntimeMigrationError,
    assert_runtime_migration_safe,
    expected_unified_runtime_rows,
    migrate_unified_runtime,
    prior_persistent_defaults,
)
from terminal_mcp.storage.sqlite import SqliteRepository

NOW = datetime(2026, 10, 17, 10, tzinfo=UTC)
START = "2026-10-17T09:59:59.123456+00:00"
END = "2026-10-17T10:04:59.987654+00:00"
DEFAULTS = SlotPolicy(300, 30, True, 20, 10)


@pytest_asyncio.fixture
async def store(tmp_path):
    path = tmp_path / "runtime.db"
    await SqliteRepository(path, tmp_path / "output.db").initialize()
    return AccessMeshStore(
        path,
        local_node_id="a",
        trusted_issuers=frozenset({"a"}),
        proof_key=b"test-runtime-migration-proof-key-only",
        clock=lambda: NOW,
    )


def legacy_session(db, agent_id="old-local", source="a", deadline=END):
    db.execute(
        "INSERT INTO agent_sessions(agent_id,registered_at,last_activity_at,task_summary,"
        "intent,work_scope,state,source_instance_id,global_expires_at) "
        "VALUES(?,?,?,'keep this task','keep intent','workspace','active',?,?)",
        (agent_id, START, START, source, deadline),
    )


def old_slot(db, agent_id="old-persistent"):
    db.execute(
        "CREATE TABLE IF NOT EXISTS logical_agent_session_policies("
        "logical_agent_id TEXT PRIMARY KEY,revision INTEGER,policy_json TEXT,updated_at TEXT)"
    )
    db.execute(
        "INSERT INTO logical_agents(logical_agent_id,display_name,state,authority_node_id,"
        "authority_epoch,created_at,updated_at) VALUES(?,'Long task','active','a',3,?,?)",
        (agent_id, START, START),
    )
    db.execute(
        "INSERT INTO logical_agent_work_sessions(work_session_id,logical_agent_id,session_epoch,"
        "authority_node_id,authority_epoch,started_at,hard_expires_at,auth_generation,state) "
        "VALUES('old-session',?,7,'a',3,?,?,4,'active')",
        (agent_id, START, END),
    )
    db.execute(
        "INSERT INTO logical_agent_session_policies VALUES(?,5,?,?)",
        (
            agent_id,
            json.dumps(
                {
                    "default_duration_seconds": 700,
                    "rearm_after_seconds": 80,
                    "warning_before_expiry_seconds": 100,
                    "draining_before_expiry_seconds": 20,
                }
            ),
            START,
        ),
    )


def migrate(store, **kwargs):
    return migrate_unified_runtime(
        store, defaults=DEFAULTS, existing_installation=True, now=NOW, **kwargs
    )


def rows(db, table, columns=None):
    columns = columns or [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
    names = ",".join(f'"{c}"' for c in columns)
    return Counter(
        hashlib.sha256(
            json.dumps(list(row), ensure_ascii=True, separators=(",", ":")).encode()
        ).hexdigest()
        for row in db.execute(f'SELECT {names} FROM "{table}"')
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("primary", ["a", "other"])
async def test_import_keeps_logical_ids_sessions_deadlines_and_policy(store, primary):
    if primary != "a":
        store.trusted_issuers = frozenset({"a", primary})
        store.install_registry_scope(mesh_id="previous-mesh", authority_id=primary)
    with store._connect() as db:
        legacy_session(db)
        legacy_session(db, "old-foreign", "former-peer")
        old_slot(db)
        original_agents = rows(db, "logical_agents")
        original_sessions = rows(db, "logical_agent_work_sessions")
    receipt = migrate(store)
    assert len(receipt["imported"]) == 2
    native = store.slot_for_agent("old-persistent")
    assert native.kind == "persistent"
    assert native.policy == SlotPolicy(700, 80, True, 100, 20)
    assert native.inherit_policy is False
    temporary = store.slot_for_agent("old-local")
    assert temporary.kind == "temporary" and temporary.policy.release_on_end
    assert not temporary.policy.rearm_enabled
    assert store.slot_for_agent("old-foreign") is None
    with store._connect() as db:
        assert not original_agents - rows(db, "logical_agents")
        assert not original_sessions - rows(db, "logical_agent_work_sessions")
        mapped = db.execute(
            "SELECT work_session_id,session_epoch FROM access_mesh_local_sessions WHERE slot_id=?",
            (native.slot_id,),
        ).fetchone()
        assert tuple(mapped) == ("old-session", 7)
        assert tuple(
            db.execute(
                "SELECT state,end_reason,task_summary FROM agent_sessions "
                "WHERE agent_id='old-local'"
            ).fetchone()
        ) == ("forced", MIGRATION_REASON, "keep this task")


@pytest.mark.asyncio
async def test_receipt_is_idempotent_after_leaving_the_old_namespace(store):
    with store._connect() as db:
        old_slot(db)
    receipt = migrate(store)
    with store._connect() as db:
        # Namespace cleanup removes native owners; old history remains available.
        db.execute("DELETE FROM access_mesh_native_owners")
        db.execute("DELETE FROM access_mesh_slot_replicas")
    assert migrate(store) == receipt
    assert store.slot_for_agent("old-persistent") is None


@pytest.mark.asyncio
async def test_unmapped_active_execution_fails_before_startup_recovery(store):
    with store._connect() as db:
        db.execute("INSERT INTO commands(hash,cmd,status) VALUES('live','printf keep','running')")
        db.execute(
            "INSERT INTO command_agent_attribution "
            "VALUES('live','old-local',?,'run','keep',NULL,NULL,NULL)",
            (START,),
        )
    with pytest.raises(RuntimeMigrationError, match="legacy_active_work_requires_drain"):
        assert_runtime_migration_safe(store.path)
    with store._connect() as db:
        assert db.execute("SELECT status FROM commands").fetchone()[0] == "running"


@pytest.mark.asyncio
async def test_missing_deadline_rolls_back_without_granting_a_new_cycle(store):
    with store._connect() as db:
        legacy_session(db, deadline=None)
    with pytest.raises(RuntimeMigrationError, match="legacy_session_deadline_missing"):
        migrate(store)
    with store._connect() as db:
        assert db.execute("SELECT state FROM agent_sessions").fetchone()[0] == "active"
        assert db.execute("SELECT COUNT(*) FROM access_mesh_native_owners").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (MIGRATION_TABLE,)
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_preservation_receipt_does_not_exempt_unrelated_row_changes(store):
    with store._connect() as db:
        legacy_session(db)
        db.execute(
            "INSERT INTO work_items(namespace,task_id,title,lane,state,state_changed_at,"
            "created_at,updated_at) VALUES('default','retained','Keep task','general',"
            "'in_progress',?,?,?)",
            (START, START, START),
        )
        db.execute(
            "INSERT INTO work_claims(namespace,task_id,agent_id,claimed_at,owner_kind,owner_id) "
            "VALUES('default','retained','old-local',?,'legacy_session','old-local')",
            (START,),
        )
        db.commit()
        before = sqlite3.connect(":memory:")
        db.backup(before)
    migrate(store)
    with store._connect() as after:
        columns = [r[1] for r in before.execute("PRAGMA table_info(agent_sessions)")]
        expected = expected_unified_runtime_rows(before, after, "agent_sessions", columns)
        assert expected == rows(after, "agent_sessions", columns)
        claim_columns = [r[1] for r in before.execute("PRAGMA table_info(work_claims)")]
        expected_claims = expected_unified_runtime_rows(before, after, "work_claims", claim_columns)
        assert expected_claims == rows(after, "work_claims", claim_columns)
        assert after.execute("SELECT owner_kind FROM work_claims").fetchone()[0] == "logical_agent"
        after.execute("UPDATE work_claims SET claim_intent='unrelated change'")
        assert expected_claims - rows(after, "work_claims", claim_columns)
        after.execute("UPDATE agent_sessions SET task_summary='unrelated tampering'")
        assert expected - rows(after, "agent_sessions", columns)
    before.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("retained_required_flag", [False, True])
async def test_hot_application_lifespan_preserves_local_policy_and_registration(
    tmp_path, retained_required_flag
):
    from terminal_mcp.app import create_app
    from terminal_mcp.auth.foundation import AuthFoundationStore
    from terminal_mcp.auth.identity_migration import initialize_fresh_identity
    from terminal_mcp.auth.pairing import PairingStore
    from terminal_mcp.auth.storage import OAuthStore
    from terminal_mcp.config import Settings

    configured = Settings(
        _env_file=None,
        database_path=tmp_path / "runtime.db",
        auth_database_path=tmp_path / "auth.db",
        output_cache_path=tmp_path / "output.db",
        runtime_config_path=tmp_path / "runtime.env",
        env_file_path=tmp_path / "install.env",
        log_path=tmp_path / "runtime.log",
        cwd=tmp_path,
        metrics_port=0,
        fleet_instance_id="a",
        persistent_session_duration_sec=700,
        persistent_session_rearm_after_sec=80,
        persistent_session_warning_after_sec=600,
        persistent_session_alert_after_sec=680,
    )
    await SqliteRepository(configured.database_path, configured.output_cache_path).initialize()
    await AuthFoundationStore(configured.auth_database_path).initialize()
    await OAuthStore(configured.auth_database_path).initialize()
    await PairingStore(configured.auth_database_path).initialize()
    initialize_fresh_identity(configured.database_path, configured.auth_database_path)
    with sqlite3.connect(configured.database_path) as db:
        if retained_required_flag:
            db.execute(
                "CREATE TABLE access_mesh_operator_defaults("
                "singleton INTEGER PRIMARY KEY,revision INTEGER NOT NULL,"
                "policy_json TEXT NOT NULL,legacy_enabled INTEGER NOT NULL,"
                "inherit_policy INTEGER NOT NULL DEFAULT 0,parent_json TEXT)"
            )
        old_slot(db)
        db.execute("UPDATE logical_agent_work_sessions SET state='ended',ended_at=?", (END,))
        db.execute("DELETE FROM logical_agent_session_policies")
    expected_policy = SlotPolicy(700, 80, True, 100, 20)
    assert prior_persistent_defaults(configured) == expected_policy

    app = create_app(configured)
    async with app.router.lifespan_context(app):
        migrated = app.state.access_mesh.store
        with migrated._connect() as db:
            assert "legacy_enabled" not in {
                row[1] for row in db.execute("PRAGMA table_info(access_mesh_operator_defaults)")
            }
            receipt = json.loads(
                db.execute(f"SELECT receipt_json FROM {MIGRATION_TABLE}").fetchone()[0]
            )
            assert receipt["preserved_local_defaults"] == asdict(expected_policy)
            assert db.execute("SELECT COUNT(*) FROM logical_agents").fetchone()[0] == 1
        (registration,) = receipt["imported"]
        assert registration["logical_agent_id"] == "old-persistent"
        slot = migrated.slot(registration["issuer_id"], registration["slot_id"])
        assert slot is not None and slot.policy == expected_policy and slot.inherit_policy
        assert migrated.slot_for_agent("old-persistent") is None
        assert migrated.defaults()["policy"] == asdict(expected_policy)
        assert migrated.defaults()["legacy_admission_enabled"] is False
        assert prior_persistent_defaults(configured) is None
        assert app.state.access_mesh_replication._task is None


@pytest.mark.asyncio
async def test_preservation_checks_the_original_deadline_independently(store):
    with store._connect() as db:
        legacy_session(db)
        db.commit()
        before = sqlite3.connect(":memory:")
        db.backup(before)
    migrate(store)
    with store._connect() as after:
        receipt = json.loads(
            after.execute(f"SELECT receipt_json FROM {MIGRATION_TABLE}").fetchone()[0]
        )
        receipt["imported"][0]["hard_expires_at"] = "2026-10-17T12:00:00+00:00"
        after.execute(f"UPDATE {MIGRATION_TABLE} SET receipt_json=?", (json.dumps(receipt),))
        columns = [r[1] for r in before.execute("PRAGMA table_info(agent_sessions)")]
        with pytest.raises(RuntimeMigrationError, match="runtime_migration_mapping_changed"):
            expected_unified_runtime_rows(before, after, "agent_sessions", columns)
    before.close()
