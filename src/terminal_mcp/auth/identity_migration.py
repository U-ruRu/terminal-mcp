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

from terminal_mcp.storage.permissions import secure_database_path


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


def _shape(conn: sqlite3.Connection, schema: str, name: str) -> tuple:
    return tuple(tuple(row[1:6]) for row in conn.execute(f'PRAGMA {schema}.table_info("{name}")'))


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


def assert_identity_isolated(main: Path, auth: Path) -> None:
    """Check before accepting any traffic; NEVER recreate old OAuth tables."""
    main, auth = check_paths(main, auth)
    if not main.exists():
        return
    # ro URI avoids creating an empty runtime database just to inspect it.
    import urllib.parse

    uri = f"file:{urllib.parse.quote(str(main))}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        if _existing(conn, "main"):
            raise IdentityMigrationError(
                "Legacy OAuth/console tables in runtime database: stop service "
                "and run offline identity migration before starting this release"
            )


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
            return {"status": "new-runtime", "source_tables": 0}
        if auth.exists():
            con.execute("ATTACH DATABASE ? AS authorization", (str(auth),))
        source = _existing(con, "runtime")
        target = _existing(con, "authorization") if auth.exists() else set()
        source_counts = _counts(con, "runtime", source)
        target_counts = _counts(con, "authorization", target) if auth.exists() else {}
        return {
            "status": "legacy-present" if source else "isolated",
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
    if reverse and not auth.exists():
        raise IdentityMigrationError("auth database does not exist")
    # Check without modifying either database. In particular, a repeat
    # forward call after success needs neither a new backup nor a write.
    state = inspect(main, auth)
    if not reverse and state["runtime_tables"] == 0:
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

    # Protect the original WAL-mode databases BEFORE converting journal modes,
    # creating the destination database or starting a cross-file transaction.
    _make_backups(main, auth, Path(backup_dir))
    if fault == "after_backup":
        raise IdentityMigrationError("injected interruption after backup")

    with closing(sqlite3.connect(main, isolation_level=None, timeout=5.0)) as con:
        con.execute("PRAGMA busy_timeout=5000")
        if con.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
            raise IdentityMigrationError("cannot enable atomic rollback journal on runtime")
        secure_database_path(auth)
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
        for name in src_tables & dest_tables:
            # Reject schema drift instead of silently dropping/mapping columns.
            if _shape(con, source, name) != _shape(con, destination, name):
                raise IdentityMigrationError("identity table schema conflict: " + name)
        # Source schema must match the implementation, including column constraints.
        with closing(sqlite3.connect(":memory:")) as expected:
            for name, ddl in SCHEMAS.items():
                expected.execute(f'CREATE TABLE "{name}" {ddl}')
                if _shape(con, source, name) != _shape(expected, "main", name):
                    raise IdentityMigrationError("unsupported legacy identity schema: " + name)
        con.execute("BEGIN EXCLUSIVE")
        try:
            for name, ddl in SCHEMAS.items():
                con.execute(f'CREATE TABLE IF NOT EXISTS {destination}."{name}" {ddl}')
                if _shape(con, source, name) != _shape(con, destination, name):
                    raise IdentityMigrationError("identity table schema conflict: " + name)
                con.execute(f'INSERT INTO {destination}."{name}" SELECT * FROM {source}."{name}"')
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
                    "INSERT INTO auth.oauth_identity_migration(version,migrated_at) VALUES(1,?)",
                    (int(time.time()),),
                )
            else:
                con.execute("DROP TABLE IF EXISTS auth.oauth_identity_migration")
            for name in reversed(tuple(SCHEMAS)):
                con.execute(f'DROP TABLE {source}."{name}"')
            for index, (table, columns) in INDEXES.items():
                con.execute(
                    f'CREATE INDEX IF NOT EXISTS {destination}."{index}" ON "{table}"({columns})'
                )
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
