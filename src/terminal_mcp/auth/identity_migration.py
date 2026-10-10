"""Offline, fail-closed relocation of OAuth and console identities.

The runtime database can be discarded; the auth database must survive.  Run
this module with the Terminal MCP service STOPPED.  Nothing is printed from
credential rows.  Forward and reverse migrations each commit both SQLite
files through a single (rollback-journal) ATTACH transaction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from terminal_mcp.auth.foundation import AUTH_SCHEMA_VERSION


class IdentityMigrationError(RuntimeError):
    """Refuse an ambiguous or unsafe identity migration."""


# These six relations form a single consistency unit.  Device exchange writes
# oauth_clients, oauth_refresh_tokens, console_devices and pairing audit together.
SCHEMAS = {
    "oauth_clients": (
        "(client_id TEXT PRIMARY KEY,"
        "client_secret_hash TEXT,"
        "redirect_uris TEXT NOT NULL,"
        "client_name TEXT NOT NULL,"
        "auth_method TEXT NOT NULL,"
        "created_at INTEGER NOT NULL)"
    ),
    "oauth_codes": (
        "(code_hash TEXT PRIMARY KEY,"
        "client_id TEXT NOT NULL,"
        "redirect_uri TEXT NOT NULL,"
        "scope TEXT NOT NULL,"
        "code_challenge TEXT NOT NULL,"
        "expires_at INTEGER NOT NULL,"
        "used INTEGER NOT NULL DEFAULT 0)"
    ),
    "oauth_refresh_tokens": (
        "(token_hash TEXT PRIMARY KEY,"
        "client_id TEXT NOT NULL,"
        "scope TEXT NOT NULL,"
        "expires_at INTEGER NOT NULL,"
        "revoked INTEGER NOT NULL DEFAULT 0)"
    ),
    "console_pairings": (
        "(secret_hash TEXT PRIMARY KEY,"
        "created_at INTEGER NOT NULL,"
        "expires_at INTEGER NOT NULL,"
        "consumed_at INTEGER)"
    ),
    "console_devices": (
        "(device_id TEXT PRIMARY KEY,"
        "client_id TEXT NOT NULL UNIQUE,"
        "public_key TEXT NOT NULL,"
        "label TEXT NOT NULL,"
        "created_at INTEGER NOT NULL,"
        "last_used_at INTEGER NOT NULL,"
        "revoked_at INTEGER)"
    ),
    "console_pairing_audit": (
        "(id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "event_type TEXT NOT NULL,"
        "outcome TEXT NOT NULL,"
        "reason TEXT NOT NULL DEFAULT '',"
        "device_id TEXT,"
        "created_at INTEGER NOT NULL)"
    ),
}
PRIMARY_KEYS = {
    "oauth_clients": "client_id",
    "oauth_codes": "code_hash",
    "oauth_refresh_tokens": "token_hash",
    "console_pairings": "secret_hash",
    "console_devices": "device_id",
    "console_pairing_audit": "id",
}
INDEXES = {
    "ix_oauth_codes_retention": ("oauth_codes", "used,expires_at"),
    "ix_oauth_refresh_retention": ("oauth_refresh_tokens", "revoked,expires_at"),
    "ix_console_pairings_expires": ("console_pairings", "expires_at"),
    "ix_console_devices_client": ("console_devices", "client_id"),
    "ix_console_pairing_audit_created": ("console_pairing_audit", "created_at"),
}
MARKER = "oauth_identity_migration"

FOUNDATION_TABLES = frozenset(
    {
        "auth_security_state",
        "auth_realms",
        "auth_principals",
        "auth_clients",
        "auth_grants",
        "auth_audit",
        "auth_access_slots",
        "auth_access_codes",
        "auth_access_code_tombstones",
        "auth_provider_bindings",
    }
)
MIGRATION_SCHEMA = "(version INTEGER PRIMARY KEY, migrated_at INTEGER NOT NULL)"
MIGRATION_VERSION = 1


def check_paths(main: Path, auth: Path) -> tuple[Path, Path]:
    main, auth = Path(main).resolve(), Path(auth).resolve()
    if main == auth or (main.exists() and auth.exists() and os.path.samefile(main, auth)):
        raise IdentityMigrationError("runtime and auth databases must be physically distinct")
    return main, auth


def _existing(conn: sqlite3.Connection, schema: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type='table' "
            f"AND name IN ({','.join('?' for _ in SCHEMAS)})",
            tuple(SCHEMAS),
        )
    }


def _database_tables(conn: sqlite3.Connection, schema: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type='table'")
    }


def _ddl_fingerprint(sql: str) -> str:
    # All six schemas have no whitespace-bearing SQL string literals.
    # Normalize formatting only; retain every CHECK, UNIQUE, default and FK.
    if not sql:
        return ""
    compact = "".join(c for c in sql.lower() if not c.isspace() and c not in '"[]')
    return compact.replace("ifnotexists", "")


def _object_sql(conn: sqlite3.Connection, schema: str, kind: str, name: str) -> str:
    row = conn.execute(
        f"SELECT sql FROM {schema}.sqlite_master WHERE type=? AND name=?",
        (kind, name),
    ).fetchone()
    return row[0] if row and row[0] else ""


def _assert_table_schemas(
    conn: sqlite3.Connection,
    schema: str,
    *,
    require_complete: bool = False,
) -> None:
    names = _existing(conn, schema)
    if require_complete and names != set(SCHEMAS):
        raise IdentityMigrationError("protected OAuth/console schema is incomplete")
    with closing(sqlite3.connect(":memory:")) as expected:
        for table in names:
            expected.execute(f'CREATE TABLE "{table}" {SCHEMAS[table]}')
            if _ddl_fingerprint(_object_sql(conn, schema, "table", table)) != _ddl_fingerprint(
                _object_sql(expected, "main", "table", table)
            ):
                raise IdentityMigrationError("identity table DDL conflict: " + table)
        for index, (table, columns) in INDEXES.items():
            if table not in names:
                continue
            expected.execute(f'CREATE INDEX "{index}" ON "{table}"({columns})')
            if _ddl_fingerprint(_object_sql(conn, schema, "index", index)) != _ddl_fingerprint(
                _object_sql(expected, "main", "index", index)
            ):
                raise IdentityMigrationError("identity index DDL conflict: " + index)
        # An undeclared explicit index can impose additional UNIQUE or partial
        # constraints even when all required indexes are present. Reject drift.
        for table in names:
            actual_indexes = {
                row[1]
                for row in conn.execute(f'PRAGMA {schema}.index_list("{table}")')
                if row[3] == "c"
            }
            required_indexes = {name for name, (owner, _) in INDEXES.items() if owner == table}
            if actual_indexes != required_indexes:
                raise IdentityMigrationError("unexpected identity indexes: " + table)


def _assert_foundation(conn: sqlite3.Connection, schema: str, auth_path: Path) -> None:
    """Read-only validation: never recreate or upgrade missing protected auth."""
    if not FOUNDATION_TABLES.issubset(_database_tables(conn, schema)):
        raise IdentityMigrationError("protected AuthFoundation tables are missing")
    version = conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0]
    if version != AUTH_SCHEMA_VERSION:
        raise IdentityMigrationError("protected AuthFoundation schema version mismatch")
    singleton = conn.execute(
        f"SELECT singleton,generation FROM {schema}.auth_security_state WHERE singleton=1"
    ).fetchone()
    if singleton is None or singleton[1] < 0:
        raise IdentityMigrationError("protected AuthFoundation security state is missing")
    if not auth_path.with_name(auth_path.name + ".access-key").is_file():
        raise IdentityMigrationError("protected Access verifier key is missing")
    if conn.execute(f"PRAGMA {schema}.quick_check").fetchone()[0] != "ok":
        raise IdentityMigrationError("protected AuthFoundation integrity failure")
    if conn.execute(f"PRAGMA {schema}.foreign_key_check").fetchone() is not None:
        raise IdentityMigrationError("protected AuthFoundation foreign-key violation")


def _assert_marker(conn: sqlite3.Connection, schema: str) -> None:
    with closing(sqlite3.connect(":memory:")) as expected:
        expected.execute(f'CREATE TABLE "{MARKER}" {MIGRATION_SCHEMA}')
        required = _object_sql(expected, "main", "table", MARKER)
    if _ddl_fingerprint(_object_sql(conn, schema, "table", MARKER)) != _ddl_fingerprint(required):
        raise IdentityMigrationError("OAuth isolation migration marker is missing or invalid")
    rows = conn.execute(f'SELECT version FROM {schema}."{MARKER}"').fetchall()
    if rows != [(MIGRATION_VERSION,)]:
        raise IdentityMigrationError("OAuth isolation migration has not completed")


def _assert_authoritative_auth(conn: sqlite3.Connection, schema: str, auth: Path) -> None:
    _assert_foundation(conn, schema, auth)
    _assert_table_schemas(conn, schema, require_complete=True)
    _assert_marker(conn, schema)


def _audit_sequence(conn: sqlite3.Connection, schema: str) -> int | None:
    if "console_pairing_audit" not in _existing(conn, schema):
        return None
    if "sqlite_sequence" not in _database_tables(conn, schema):
        raise IdentityMigrationError("audit AUTOINCREMENT state is missing")
    rows = conn.execute(
        f"SELECT seq FROM {schema}.sqlite_sequence WHERE name='console_pairing_audit'"
    ).fetchall()
    if len(rows) > 1:
        raise IdentityMigrationError("ambiguous audit AUTOINCREMENT sequence")
    value = int(rows[0][0]) if rows else None
    highest = conn.execute(
        f"SELECT COALESCE(MAX(id),0) FROM {schema}.console_pairing_audit"
    ).fetchone()[0]
    if (value is None and highest) or (value is not None and value < highest):
        raise IdentityMigrationError("invalid audit AUTOINCREMENT high-water mark")
    return value


def _copy_audit_sequence(conn: sqlite3.Connection, source: str, dest: str) -> None:
    original = _audit_sequence(conn, source)
    copied = _audit_sequence(conn, dest)
    if original is None:
        if copied is not None:
            raise IdentityMigrationError("audit destination sequence conflict")
        return
    if copied is not None and copied > original:
        raise IdentityMigrationError("audit destination sequence exceeds source")
    conn.execute(f"DELETE FROM {dest}.sqlite_sequence WHERE name='console_pairing_audit'")
    conn.execute(
        f"INSERT INTO {dest}.sqlite_sequence(name,seq) VALUES(?,?)",
        ("console_pairing_audit", original),
    )
    if _audit_sequence(conn, dest) != original:
        raise IdentityMigrationError("audit high-water-mark verification failed")


def _open_readonly(path: Path) -> sqlite3.Connection:
    import urllib.parse

    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5.0)


def _fresh_install(main: Path, auth: Path) -> bool:
    # A previously configured node keeps its verifier key through clean start.
    verifier = auth.with_name(auth.name + ".access-key")
    if auth.exists() or verifier.exists():
        return False
    if not main.exists():
        return True
    # Access Mesh composition may eagerly create its EMPTY metadata tables
    # before FastAPI lifespan starts. That is a pristine first install, not
    # evidence of a previously authorized runtime. Any other working tables,
    # any legacy OAuth table or a nonempty Mesh table block fresh bootstrap.
    with closing(_open_readonly(main)) as db:
        for table in _database_tables(db, "main"):
            if table == "sqlite_sequence":
                # Deleted historical Access Mesh rows may leave this proof
                # that the working DB is NOT from a pristine first install.
                if db.execute("SELECT 1 FROM sqlite_sequence WHERE seq>0 LIMIT 1").fetchone():
                    return False
                continue
            if not table.startswith("access_mesh_"):
                return False
            if db.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                return False
    return True


def initialize_fresh_identity(main: Path, auth: Path) -> None:
    """Seal a pristine install after initializing its three identity stores."""
    main, auth = check_paths(main, auth)
    if not auth.is_file():
        raise IdentityMigrationError("fresh AuthFoundation was not created")
    with closing(sqlite3.connect(auth, timeout=5.0)) as db:
        _assert_foundation(db, "main", auth)
        _assert_table_schemas(db, "main", require_complete=True)
        if _object_sql(db, "main", "table", MARKER):
            raise IdentityMigrationError("fresh identity marker already exists")
        with db:
            db.execute(f'CREATE TABLE "{MARKER}" {MIGRATION_SCHEMA}')
            db.execute(
                f'INSERT INTO "{MARKER}"(version,migrated_at) VALUES(?,?)',
                (MIGRATION_VERSION, int(time.time())),
            )
        _assert_authoritative_auth(db, "main", auth)


def _counts(conn: sqlite3.Connection, schema: str, names: set[str]) -> dict[str, int]:
    return {
        name: conn.execute(f'SELECT count(*) FROM {schema}."{name}"').fetchone()[0]
        for name in sorted(names)
    }


def _fingerprint(conn: sqlite3.Connection, schema: str) -> str:
    digest = hashlib.sha256()
    for name, primary_key in PRIMARY_KEYS.items():
        digest.update(name.encode() + b"\x00")
        rows = conn.execute(f'SELECT * FROM {schema}."{name}" ORDER BY "{primary_key}"')
        for row in rows:
            digest.update(json.dumps(tuple(row), ensure_ascii=True, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def assert_identity_isolated(main: Path, auth: Path) -> bool | None:
    """Check identity continuity before serving; return True only for fresh install."""
    main, auth = check_paths(main, auth)
    if _fresh_install(main, auth):
        return True
    if not auth.is_file():
        raise IdentityMigrationError("protected auth database is missing")
    if main.exists():
        with closing(_open_readonly(main)) as runtime:
            if _existing(runtime, "main"):
                raise IdentityMigrationError(
                    "Legacy OAuth/console tables in runtime database: stop service "
                    "and run offline identity migration before starting this release"
                )
    with closing(_open_readonly(auth)) as protected:
        _assert_authoritative_auth(protected, "main", auth)
    return None


def _backup_file(source: Path, target: Path) -> None:
    if not source.exists():
        return
    if target.exists():
        raise IdentityMigrationError("backup destination already exists; refusing overwrite")
    with closing(sqlite3.connect(source)) as live:
        with closing(sqlite3.connect(target)) as snapshot:
            live.backup(snapshot)
            result = snapshot.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise IdentityMigrationError("backup integrity check failed")
    os.chmod(target, 0o600)


def _make_backups(main: Path, auth: Path, backup_dir: Path) -> None:
    # Nothing is removed or changed until BOTH consistent snapshots are sealed.
    backup_dir = Path(backup_dir).resolve()
    if backup_dir.exists():
        if any(backup_dir.iterdir()):
            raise IdentityMigrationError("backup directory is not empty; use a fresh directory")
    else:
        backup_dir.mkdir(parents=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    _backup_file(main, backup_dir / "runtime.before.sqlite3")
    _backup_file(auth, backup_dir / "auth.before.sqlite3")
    (backup_dir / "README.txt").write_text(
        "Offline pre-migration SQLite snapshots. Preserve the corresponding "
        "configuration, access verifier key, environment and service release. "
        "These files are sensitive; do not commit or attach them to reports.\n",
        encoding="utf-8",
    )
    os.chmod(backup_dir / "README.txt", 0o600)


def inspect(main: Path, auth: Path) -> dict:
    main, auth = check_paths(main, auth)
    with closing(sqlite3.connect(":memory:")) as con:
        if main.exists():
            con.execute("ATTACH DATABASE ? AS runtime", (str(main),))
        else:
            if _fresh_install(main, auth):
                return {"status": "new-install", "runtime_tables": 0, "auth_tables": 0}
            if not auth.is_file():
                return {"status": "invalid-protected-auth", "runtime_tables": 0}
            with closing(_open_readonly(auth)) as protected:
                tables = _existing(protected, "main")
                try:
                    _assert_authoritative_auth(protected, "main", auth)
                    state = "isolated"
                except (IdentityMigrationError, sqlite3.Error):
                    state = "invalid-protected-auth"
                return {"status": state, "runtime_tables": 0, "auth_tables": len(tables)}
        if auth.exists():
            con.execute("ATTACH DATABASE ? AS authorization", (str(auth),))
        source = _existing(con, "runtime")
        target = _existing(con, "authorization") if auth.exists() else set()
        source_counts = _counts(con, "runtime", source)
        target_counts = _counts(con, "authorization", target) if auth.exists() else {}
        if source:
            state = "legacy-present"
        elif not auth.is_file():
            state = "invalid-protected-auth"
        else:
            try:
                _assert_authoritative_auth(con, "authorization", auth)
                state = "isolated"
            except (IdentityMigrationError, sqlite3.Error):
                state = "invalid-protected-auth"
        return {
            "status": state,
            "runtime_tables": len(source),
            "auth_tables": len(target),
            "runtime_rows": sum(source_counts.values()),
            "auth_rows": sum(target_counts.values()),
        }


def migrate(
    main: Path,
    auth: Path,
    backup_dir: Path,
    *,
    reverse: bool = False,
    fault: str | None = None,
) -> dict:
    """Transfer all identities without changing credential values or TTL.

    A service-level offline boundary is mandatory. SQLite rollback journals
    (rather than WAL) enable its super-journal atomic commit for both files.
    Existing destination rows cause a hard conflict; no INSERT OR REPLACE.
    Reverse copies CURRENT auth state, including post-cutover revocations.
    """
    main, auth = check_paths(main, auth)
    if not main.exists():
        raise IdentityMigrationError("runtime database does not exist")
    if not auth.is_file():
        raise IdentityMigrationError("protected auth database is missing")
    # Validate the durable foundation *before any backup or journal changes*.
    # Merely checking a path is insufficient: a missing file must never be
    # replaced by a new SQLite DB without the original grants/principals.
    with closing(_open_readonly(auth)) as protected:
        _assert_foundation(protected, "main", auth)
    # Check without modifying either database. In particular, a repeat
    # forward call after success needs neither a new backup nor a write.
    state = inspect(main, auth)
    if not reverse and state["runtime_tables"] == 0:
        with closing(_open_readonly(auth)) as protected:
            _assert_authoritative_auth(protected, "main", auth)
        return {**state, "status": "already-isolated"}
    if reverse and state["auth_tables"] == 0:
        raise IdentityMigrationError("no auth identity tables available to reverse")
    if reverse and state["runtime_tables"] != 0:
        raise IdentityMigrationError("rollback target has legacy identity tables; conflict")
    if not reverse and state["runtime_tables"] != len(SCHEMAS):
        raise IdentityMigrationError("incomplete legacy identity schema; refusing partial copy")
    if reverse and state["auth_tables"] != len(SCHEMAS):
        raise IdentityMigrationError("incomplete auth identity schema; refusing partial copy")
    if not reverse and state["auth_rows"]:
        raise IdentityMigrationError("auth destination already contains identities; conflict")
    if reverse and state["runtime_rows"]:
        raise IdentityMigrationError("runtime destination already contains identities; conflict")

    # Complete DDL and required indexes are validated, not only PRAGMA
    # table_info (which omits UNIQUE, CHECK, and AUTOINCREMENT clauses).
    with closing(sqlite3.connect(":memory:")) as probe:
        probe.execute("ATTACH DATABASE ? AS runtime", (str(main),))
        probe.execute("ATTACH DATABASE ? AS protected", (str(auth),))
        source = "protected" if reverse else "runtime"
        destination = "runtime" if reverse else "protected"
        _assert_table_schemas(probe, source, require_complete=True)
        _assert_table_schemas(probe, destination)
        # Even if rows were deleted, a prior audit sequence is authoritative.
        if "console_pairing_audit" in _existing(probe, destination):
            if _audit_sequence(probe, destination) is not None:
                raise IdentityMigrationError("existing audit sequence in destination")
        if reverse:
            _assert_marker(probe, "protected")
        elif _object_sql(probe, "protected", "table", MARKER):
            raise IdentityMigrationError("existing migration marker; manual review required")
        _audit_sequence(probe, source)

    # Protect the original WAL-mode databases BEFORE converting journal modes,
    # creating the destination database or starting a cross-file transaction.
    _make_backups(main, auth, Path(backup_dir))
    if fault == "after_backup":
        raise IdentityMigrationError("injected interruption after backup")

    with closing(sqlite3.connect(main, isolation_level=None, timeout=5.0)) as con:
        con.execute("PRAGMA busy_timeout=5000")
        if con.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
            raise IdentityMigrationError("cannot enable atomic rollback journal on runtime")
        # SQLite cannot always change a WAL database's journal mode while it
        # is ATTACHed to another connection. Convert it in its own connection.
        auth_conn = sqlite3.connect(auth, isolation_level=None, timeout=5.0)
        try:
            auth_conn.execute("PRAGMA busy_timeout=5000")
            mode = auth_conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if mode.lower() != "delete":
                raise IdentityMigrationError("cannot enable atomic rollback journal on auth")
        finally:
            auth_conn.close()
        con.execute("ATTACH DATABASE ? AS auth", (str(auth),))
        source = "auth" if reverse else "main"
        destination = "main" if reverse else "auth"
        src_tables = _existing(con, source)
        dest_tables = _existing(con, destination)
        if src_tables != set(SCHEMAS) or _counts(con, destination, dest_tables) != {
            name: 0 for name in dest_tables
        }:
            raise IdentityMigrationError("source or destination changed before backup")
        _assert_foundation(con, "auth", auth)
        _assert_table_schemas(con, source, require_complete=True)
        _assert_table_schemas(con, destination)
        con.execute("BEGIN EXCLUSIVE")
        try:
            for name, ddl in SCHEMAS.items():
                con.execute(f'CREATE TABLE IF NOT EXISTS {destination}."{name}" {ddl}')
                con.execute(f'INSERT INTO {destination}."{name}" SELECT * FROM {source}."{name}"')
            _copy_audit_sequence(con, source, destination)
            for index, (table, columns) in INDEXES.items():
                con.execute(
                    f'CREATE INDEX IF NOT EXISTS {destination}."{index}" ON "{table}"({columns})'
                )
            _assert_table_schemas(con, destination, require_complete=True)
            if _fingerprint(con, source) != _fingerprint(con, destination):
                raise IdentityMigrationError("copied identity verification failed")
            if not reverse:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS auth.oauth_identity_migration("
                    "version INTEGER PRIMARY KEY, migrated_at INTEGER NOT NULL)"
                )
                if con.execute("SELECT 1 FROM auth.oauth_identity_migration").fetchone():
                    raise IdentityMigrationError(
                        "existing migration marker; manual review required"
                    )
                con.execute(
                    "INSERT INTO auth.oauth_identity_migration(version,migrated_at) VALUES(?,?)",
                    (MIGRATION_VERSION, int(time.time())),
                )
            else:
                con.execute("DROP TABLE IF EXISTS auth.oauth_identity_migration")
            for name in reversed(tuple(SCHEMAS)):
                con.execute(f'DROP TABLE {source}."{name}"')
            if fault == "before_commit":
                raise IdentityMigrationError("injected interruption before commit")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    return {
        "status": "reversed" if reverse else "migrated",
        "tables": len(SCHEMAS),
        "rows": state["auth_rows"] if reverse else state["runtime_rows"],
        "backup": str(Path(backup_dir).resolve()),
    }


def clean_working_data(
    main: Path,
    auth: Path,
    output: Path,
    quarantine_dir: Path,
) -> dict:
    """Move only disposable runtime/output SQLite files after migration.

    A current consistent auth snapshot is retained for disaster recovery.
    It must NOT overwrite newer auth data on rollback.
    """
    main, auth = check_paths(main, auth)
    output = Path(output).resolve()
    if (
        output == main
        or output == auth
        or (
            output.exists()
            and (
                (main.exists() and os.path.samefile(output, main))
                or (auth.exists() and os.path.samefile(output, auth))
            )
        )
    ):
        raise IdentityMigrationError("runtime, auth and output paths must be distinct")
    if not auth.exists():
        raise IdentityMigrationError("protected auth database is missing")
    assert_identity_isolated(main, auth)
    quarantine_dir = Path(quarantine_dir).resolve()
    if quarantine_dir.exists():
        raise IdentityMigrationError("quarantine destination already exists")
    # All renames must be atomic; detect a cross-filesystem output-cache path
    # BEFORE moving even one working file or creating the quarantine.
    destination_parent = quarantine_dir.parent
    while not destination_parent.exists():
        destination_parent = destination_parent.parent
    destination_device = destination_parent.stat().st_dev
    for path in (main, output):
        for suffix in ("", "-wal", "-shm"):
            origin = Path(str(path) + suffix)
            if origin.exists() and origin.stat().st_dev != destination_device:
                raise IdentityMigrationError(
                    "clean quarantine must be on the same filesystem as runtime and output"
                )
    quarantine_dir.mkdir(parents=True, mode=0o700)
    os.chmod(quarantine_dir, 0o700)
    _backup_file(auth, quarantine_dir / "auth.before.sqlite3")
    moved: list[tuple[Path, Path]] = []
    try:
        for label, path in (("runtime", main), ("output", output)):
            for suffix in ("", "-wal", "-shm"):
                origin = Path(str(path) + suffix)
                if not origin.exists():
                    continue
                destination = quarantine_dir / (label + ".sqlite3" + suffix)
                origin.rename(destination)  # Atomic on the same filesystem.
                moved.append((origin, destination))
        manifest = {
            "runtime_path": str(main),
            "output_path": str(output),
            "auth_path": str(auth),
            "moved_files": [dest.name for _, dest in moved],
            "warning": (
                "On rollback restore working files while stopped. NEVER replace current "
                "auth with auth.before.sqlite3 after live tokens/grants changed."
            ),
        }
        manifest_path = quarantine_dir / "MANIFEST.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
    except BaseException:
        for origin, destination in reversed(moved):
            destination.rename(origin)
        raise
    return {"status": "cleaned", "moved_files": len(moved), "quarantine": str(quarantine_dir)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Terminal MCP offline OAuth identity migration")
    parser.add_argument("action", choices=("inspect", "forward", "reverse", "clean"))
    parser.add_argument("--main", required=True, type=Path, help="runtime SQLite database")
    parser.add_argument("--auth", required=True, type=Path, help="protected auth SQLite database")
    parser.add_argument("--output", type=Path, help="disposable SQLite output cache (clean only)")
    parser.add_argument("--backup-dir", type=Path, help="NEW protected snapshot directory")
    parser.add_argument(
        "--service-stopped",
        action="store_true",
        help="explicit confirmation that no Terminal MCP worker can write either SQLite file",
    )
    args = parser.parse_args()
    try:
        if args.action == "inspect":
            result = inspect(args.main, args.auth)
        else:
            if not args.service_stopped or args.backup_dir is None:
                parser.error("mutation requires --service-stopped and --backup-dir")
            if args.action == "clean":
                if args.output is None:
                    parser.error("clean requires --output")
                result = clean_working_data(args.main, args.auth, args.output, args.backup_dir)
            else:
                result = migrate(
                    args.main, args.auth, args.backup_dir, reverse=args.action == "reverse"
                )
        print(json.dumps(result, sort_keys=True))
    except (IdentityMigrationError, sqlite3.Error, OSError) as exc:
        parser.exit(2, f"identity migration refused: {exc}\n")


if __name__ == "__main__":
    main()
