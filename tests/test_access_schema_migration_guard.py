"""Isolated Access schema fencing, interrupted startup, and legacy migration origin."""

import ast
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from terminal_mcp.auth.identity_migration import IdentityMigrationError, assert_identity_isolated
from terminal_mcp.core import access_mesh_grants
from terminal_mcp.core.access_mesh_grants import (
    ACCESS_MESH_SCHEMA_VERSION,
    LocalAccessMesh,
    SlotPolicy,
)
from terminal_mcp.storage import claim_leases
from terminal_mcp.storage import sqlite as repository_module
from terminal_mcp.storage.access_mesh import AccessMeshStore
from terminal_mcp.storage.sqlite import SCHEMA_VERSION, SqliteRepository

MESH_ARGS = {
    "local_node_id": "issuer",
    "trusted_issuers": frozenset({"issuer"}),
    "proof_key": b"synthetic-schema-guard-fixture-key",
}
OLD_POLICY = json.dumps(
    {
        "duration_seconds": 1380,
        "cooldown_seconds": 60,
        "rearm_enabled": True,
        "warning_seconds": 120,
        "draining_seconds": 30,
        "release_on_end": True,
    },
    sort_keys=True,
)


def legacy_database(path, *, version=24):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE access_mesh_slot_replicas (
                issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL,
                logical_agent_id TEXT NOT NULL, kind TEXT NOT NULL,
                state TEXT NOT NULL, revision INTEGER NOT NULL,
                event_id TEXT NOT NULL, policy_json TEXT NOT NULL,
                code_tag TEXT NOT NULL, anchor TEXT,
                agent_name TEXT, display_suffix TEXT, deadline_at TEXT,
                fence_revision INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(issuer_id,slot_id)
            ) WITHOUT ROWID;
            CREATE TABLE access_mesh_operator_defaults (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                revision INTEGER NOT NULL, policy_json TEXT NOT NULL,
                legacy_enabled INTEGER NOT NULL
            );
        """)
        db.execute(
            "INSERT INTO access_mesh_slot_replicas "
            "(issuer_id,slot_id,logical_agent_id,kind,state,revision,event_id,"
            "policy_json,code_tag,anchor) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "issuer",
                "slot",
                "agent",
                "persistent",
                "active",
                7,
                "event",
                OLD_POLICY,
                "synthetic-code-tag",
                None,
            ),
        )
        db.execute("INSERT INTO access_mesh_operator_defaults VALUES(1,7,?,1)", (OLD_POLICY,))
        db.execute(f"PRAGMA user_version={version}")


def inspect(path):
    with sqlite3.connect(path) as db:
        return (
            db.execute("PRAGMA user_version").fetchone()[0],
            tuple(db.iterdump()),
        )


def origin(path):
    with sqlite3.connect(path) as db:
        if (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migration_state'"
            ).fetchone()
            is None
        ):
            return None
        row = db.execute(
            "SELECT value FROM schema_migration_state WHERE name='repository_schema_from'"
        ).fetchone()
        return row[0] if row else None


def test_access_and_repository_versions_match_static_release_fingerprint():
    assert ACCESS_MESH_SCHEMA_VERSION == SCHEMA_VERSION == 25
    tree = ast.parse(Path(repository_module.__file__).read_text())
    declarations = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "SCHEMA_VERSION"
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
    ]
    assert declarations == [SCHEMA_VERSION]


def test_sync_upgrade_fences_all_five_access_columns_and_preserves_rows(tmp_path):
    path = tmp_path / "main.sqlite3"
    legacy_database(path)
    AccessMeshStore(path, **MESH_ARGS)
    assert inspect(path)[0] == 25
    assert origin(path) == 24
    with sqlite3.connect(path) as db:
        assert {"inherit_policy", "policy_window_json", "code_generation"} <= {
            row[1] for row in db.execute("PRAGMA table_info(access_mesh_slot_replicas)")
        }
        assert {"inherit_policy", "parent_json"} <= {
            row[1] for row in db.execute("PRAGMA table_info(access_mesh_operator_defaults)")
        }
        assert db.execute(
            "SELECT revision,policy_json,inherit_policy,policy_window_json,code_generation "
            "FROM access_mesh_slot_replicas"
        ).fetchone() == (7, OLD_POLICY, 0, None, 1)
        assert db.execute(
            "SELECT revision,policy_json,legacy_enabled,inherit_policy,parent_json "
            "FROM access_mesh_operator_defaults"
        ).fetchone() == (7, OLD_POLICY, 1, 0, None)


def test_failure_inside_access_ddl_rolls_back_schema_and_fence(tmp_path):
    path = tmp_path / "main.sqlite3"
    legacy_database(path)
    before = inspect(path)

    class FailSecondAlter(LocalAccessMesh):
        def _prepare_schema_upgrade(self, db, *, from_version):
            assert from_version == 24
            alters = 0

            def authorize(action, _first, _second, _database, _source):
                nonlocal alters
                if action == sqlite3.SQLITE_ALTER_TABLE:
                    alters += 1
                    if alters == 2:
                        return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            db.set_authorizer(authorize)

    with pytest.raises(sqlite3.DatabaseError):
        FailSecondAlter(path, **MESH_ARGS)
    assert inspect(path) == before
    LocalAccessMesh(path, **MESH_ARGS)
    assert inspect(path)[0] == 25


@pytest.mark.parametrize(
    "boundary,exit_code,expected_version",
    [
        ("inside", 77, 24),
        ("after", 78, 25),
    ],
)
def test_process_exit_is_atomic_at_first_access_commit(
    tmp_path, boundary, exit_code, expected_version
):
    path = tmp_path / "main.sqlite3"
    legacy_database(path)
    before = inspect(path)
    script = """
import os
import sys
from terminal_mcp.core.access_mesh_grants import LocalAccessMesh
class Interrupted(LocalAccessMesh):
    def _prepare_schema_upgrade(self, db, *, from_version):
        if sys.argv[2] == "inside":
            def trace(sql):
                if "ADD COLUMN policy_window_json" in sql:
                    os._exit(77)
            db.set_trace_callback(trace)
    def _initialize(self):
        super()._initialize()
        if sys.argv[2] == "after":
            os._exit(78)
Interrupted(sys.argv[1], local_node_id="issuer", trusted_issuers=frozenset({"issuer"}),
            proof_key=b"synthetic-schema-guard-fixture-key")
"""
    environment = os.environ.copy()
    source_root = str(Path(access_mesh_grants.__file__).resolve().parents[2])
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (source_root, environment.get("PYTHONPATH")))
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), boundary],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == exit_code, result.stderr
    assert inspect(path)[0] == expected_version
    if boundary == "inside":
        assert inspect(path) == before
    else:
        assert origin(path) == 24
    LocalAccessMesh(path, **MESH_ARGS)
    assert inspect(path)[0] == 25
    assert origin(path) == 24


@pytest.mark.asyncio
async def test_pre_lifespan_upgrade_is_rejected_by_schema24_repository_guard(tmp_path, monkeypatch):
    path = tmp_path / "main.sqlite3"
    legacy_database(path)
    # Reproduce failure after the synchronous constructor, before lifespan.
    LocalAccessMesh(path, **MESH_ARGS)
    before = inspect(path)
    # The accepted schema-24 repository has this same version guard. Changing
    # only its supported ceiling exercises that guard without loading old code.
    monkeypatch.setattr(repository_module, "SCHEMA_VERSION", 24)
    with pytest.raises(RuntimeError, match="database schema 25 is newer than supported 24"):
        await SqliteRepository(path).initialize()
    assert inspect(path) == before


def test_future_schema_rejected_before_any_access_schema_write(tmp_path):
    path = tmp_path / "main.sqlite3"
    legacy_database(path, version=26)
    before = inspect(path)
    with pytest.raises(RuntimeError, match="database schema 26 is newer than supported 25"):
        AccessMeshStore(path, **MESH_ARGS)
    assert inspect(path) == before


@pytest.mark.asyncio
async def test_early_fence_preserves_pre21_migration_across_failed_main_startup(
    tmp_path, monkeypatch
):
    path = tmp_path / "main.sqlite3"
    legacy_database(path, version=20)
    LocalAccessMesh(path, **MESH_ARGS)
    LocalAccessMesh(path, **MESH_ARGS)
    assert inspect(path)[0] == 25
    assert origin(path) == 20
    repo = SqliteRepository(path)
    scrub = repo._scrub_pruned_command_bodies
    monkeypatch.setattr(
        repo, "_scrub_pruned_command_bodies", AsyncMock(side_effect=RuntimeError("startup failed"))
    )
    with pytest.raises(RuntimeError, match="startup failed"):
        await repo.initialize()
    assert origin(path) == 20
    monkeypatch.setattr(repo, "_scrub_pruned_command_bodies", scrub)
    migration = AsyncMock(wraps=claim_leases.migrate_existing_claim_leases)
    monkeypatch.setattr(claim_leases, "migrate_existing_claim_leases", migration)
    await repo.initialize()
    migration.assert_awaited_once()
    assert origin(path) is None
    assert inspect(path)[0] == 25
    await repo.initialize()
    migration.assert_awaited_once()


@pytest.mark.asyncio
async def test_repository_clears_only_its_own_migration_marker(tmp_path):
    path = tmp_path / "main.sqlite3"
    legacy_database(path)
    LocalAccessMesh(path, **MESH_ARGS)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO schema_migration_state VALUES('another_pending_migration',1)")
    await SqliteRepository(path).initialize()
    assert origin(path) is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM schema_migration_state").fetchall() == [
            ("another_pending_migration", 1)
        ]


@pytest.mark.asyncio
async def test_fresh_repository_before_access_sets_same_schema_revision(tmp_path):
    path = tmp_path / "main.sqlite3"
    await SqliteRepository(path).initialize()
    assert inspect(path)[0] == 25
    AccessMeshStore(path, **MESH_ARGS)
    assert inspect(path)[0] == 25
    assert origin(path) is None


@pytest.mark.asyncio
async def test_access_disabled_upgrade_keeps_pending_native_defaults_snapshot(tmp_path):
    path = tmp_path / "main.sqlite3"
    legacy_database(path)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM access_mesh_operator_defaults")
        db.execute(
            "ALTER TABLE access_mesh_operator_defaults "
            "ADD COLUMN inherit_policy INTEGER NOT NULL DEFAULT 0"
        )
        db.execute("ALTER TABLE access_mesh_operator_defaults ADD COLUMN parent_json TEXT")
    # Access disabled: the main schema upgrade runs before its configured store.
    await SqliteRepository(path).initialize()
    assert inspect(path)[0] == 25
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT value FROM schema_migration_state "
            "WHERE name='access_defaults_snapshot_required'"
        ).fetchone() == (1,)
    native = SlotPolicy(1200, 60, True, 120, 30)
    value = AccessMeshStore(
        path, **MESH_ARGS, upgrade_defaults=native, upgrade_legacy_enabled=False
    )
    assert value.defaults()["policy"] == asdict(native)
    assert value.defaults()["legacy_enabled"] is False
    assert value.defaults()["inherit_policy"] is False
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT value FROM schema_migration_state "
                "WHERE name='access_defaults_snapshot_required'"
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_fresh_access_has_no_migration_ledger_before_or_after_main_start(tmp_path):
    path = tmp_path / "main.sqlite3"
    first = AccessMeshStore(path, **MESH_ARGS)
    assert first.defaults() is None
    assert origin(path) is None
    await SqliteRepository(path).initialize()
    second = AccessMeshStore(
        path,
        **MESH_ARGS,
        upgrade_defaults=SlotPolicy(1200, 60, True, 120, 30),
        upgrade_legacy_enabled=True,
    )
    assert second.defaults() is None
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migration_state'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("precreate_file", [False, True])
def test_fresh_access_and_pre_lifespan_restart_remain_pristine(tmp_path, precreate_file):
    path = tmp_path / "main.sqlite3"
    auth = tmp_path / "auth.sqlite3"
    if precreate_file:
        with sqlite3.connect(path):
            pass
    for _ in range(2):
        AccessMeshStore(path, **MESH_ARGS)
        assert inspect(path)[0] == 25
        with sqlite3.connect(path) as db:
            assert (
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='schema_migration_state'"
                ).fetchone()
                is None
            )
        assert assert_identity_isolated(path, auth) is True
        assert not auth.exists()


def test_fresh_failure_after_access_commit_preserves_pristine_identity_check(tmp_path):
    path = tmp_path / "main.sqlite3"
    auth = tmp_path / "auth.sqlite3"

    class LaterFailure(AccessMeshStore):
        def _initialize(self):
            super()._initialize()
            raise RuntimeError("synthetic later startup failure")

    with pytest.raises(RuntimeError, match="synthetic later startup failure"):
        LaterFailure(path, **MESH_ARGS)
    assert inspect(path)[0] == 25
    assert assert_identity_isolated(path, auth) is True
    AccessMeshStore(path, **MESH_ARGS)
    assert assert_identity_isolated(path, auth) is True


@pytest.mark.parametrize("old_version", [0, 24])
def test_existing_access_data_still_refuses_missing_protected_auth(tmp_path, old_version):
    path = tmp_path / "main.sqlite3"
    auth = tmp_path / "auth.sqlite3"
    legacy_database(path, version=old_version)
    AccessMeshStore(path, **MESH_ARGS)
    assert origin(path) == old_version
    assert inspect(path)[0] == 25
    with pytest.raises(IdentityMigrationError, match="protected auth database is missing"):
        assert_identity_isolated(path, auth)
    assert not auth.exists()


@pytest.mark.asyncio
async def test_main_first_old_persisted_defaults_need_no_empty_ledger(tmp_path):
    path = tmp_path / "main.sqlite3"
    legacy_database(path)
    await SqliteRepository(path).initialize()
    value = AccessMeshStore(path, **MESH_ARGS)
    assert value.defaults()["policy"] == json.loads(OLD_POLICY)
    assert value.defaults()["inherit_policy"] is False
    assert origin(path) is None
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migration_state'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("old_shape", [True, False])
def test_old_empty_access_only_first_start_remains_fresh(tmp_path, old_shape):
    path = tmp_path / "main.sqlite3"
    auth = tmp_path / "auth.sqlite3"
    legacy_database(path, version=0)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM access_mesh_slot_replicas")
        db.execute("DELETE FROM access_mesh_operator_defaults")
        if not old_shape:
            db.execute(
                "ALTER TABLE access_mesh_operator_defaults "
                "ADD COLUMN inherit_policy INTEGER NOT NULL DEFAULT 0"
            )
            db.execute("ALTER TABLE access_mesh_operator_defaults ADD COLUMN parent_json TEXT")
    assert assert_identity_isolated(path, auth) is True
    for _ in range(2):
        value = AccessMeshStore(
            path,
            **MESH_ARGS,
            upgrade_defaults=SlotPolicy(1200, 60, True, 120, 30),
            upgrade_legacy_enabled=True,
        )
        assert value.defaults() is None
        assert origin(path) is None
        assert inspect(path)[0] == 25
        assert assert_identity_isolated(path, auth) is True


@pytest.mark.parametrize("history", ["ordinary_table", "positive_sequence"])
def test_empty_access_tables_do_not_hide_prior_runtime_history(tmp_path, history):
    path = tmp_path / "main.sqlite3"
    auth = tmp_path / "auth.sqlite3"
    legacy_database(path, version=0)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM access_mesh_slot_replicas")
        db.execute("DELETE FROM access_mesh_operator_defaults")
        if history == "ordinary_table":
            db.execute("CREATE TABLE prior_runtime_data(value TEXT)")
        else:
            db.execute(
                "CREATE TABLE access_mesh_history(sequence INTEGER PRIMARY KEY AUTOINCREMENT)"
            )
            db.execute("INSERT INTO access_mesh_history DEFAULT VALUES")
            db.execute("DELETE FROM access_mesh_history")
    value = AccessMeshStore(
        path,
        **MESH_ARGS,
        upgrade_defaults=SlotPolicy(1200, 60, True, 120, 30),
        upgrade_legacy_enabled=True,
    )
    assert origin(path) == 0
    assert value.defaults()["inherit_policy"] is False
    with pytest.raises(IdentityMigrationError, match="protected auth database is missing"):
        assert_identity_isolated(path, auth)
