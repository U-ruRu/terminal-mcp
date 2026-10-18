"""Offline authorization retention for the Linux clean-deploy installer.

The protected auth SQLite store, lineage sidecars, credentials and auth-only
configuration survive runtime cleanup. Callers stop the service before mutation.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

from terminal_mcp.deployment.clean import _checked, _values

# The value list deliberately excludes Mesh, slots, runtime and Fleet state.
AUTH_KEYS = (
    "TERMINAL_MCP_AUTH_DATABASE_PATH",
    "TERMINAL_MCP_PUBLIC_BASE_URL",
    "TERMINAL_MCP_CONSOLE_PUBLIC_BASE_URL",
    "TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS",
    "TERMINAL_MCP_AUTH_MODE",
    "TERMINAL_MCP_MCP_AUTH_MODE",
    "TERMINAL_MCP_ACTIONS_AUTH_MODE",
    "TERMINAL_MCP_ADMIN_USERNAME",
    "TERMINAL_MCP_ADMIN_PASSWORD",
    "TERMINAL_MCP_ADMIN_SESSION_SECRET",
    "TERMINAL_MCP_OAUTH_ADMIN_USERNAME",
    "TERMINAL_MCP_OAUTH_ADMIN_PASSWORD",
    "TERMINAL_MCP_OAUTH_SIGNING_SECRET",
    "TERMINAL_MCP_OAUTH_ISSUER",
    "TERMINAL_MCP_OAUTH_AUDIENCE",
    "TERMINAL_MCP_OAUTH_JWKS_URL",
    "TERMINAL_MCP_OAUTH_REQUIRED_SCOPES",
    "TERMINAL_MCP_OAUTH_ACCESS_TTL_SEC",
    "TERMINAL_MCP_OAUTH_REFRESH_TTL_SEC",
    "TERMINAL_MCP_OAUTH_CODE_TTL_SEC",
    "TERMINAL_MCP_OAUTH_USERS_JSON",
    "TERMINAL_MCP_BEARER_CREDENTIALS_JSON",
    "TERMINAL_MCP_BEARER_TOKENS",
    "TERMINAL_MCP_OAUTH_USERNAME_FILE",
    "TERMINAL_MCP_OAUTH_PASSWORD_FILE",
)
_CONFIG = "terminal-mcp.env"
_SUFFIXES = ("", "-wal", "-shm", "-journal", ".access-key", ".identity-anchor")


def paths(config: Path, data: Path) -> tuple[dict[str, str], Path, Path, Path, tuple[Path, ...]]:
    env = config / _CONFIG
    old = _values(env)
    runtime = Path(old.get("TERMINAL_MCP_RUNTIME_CONFIG_PATH") or config / "runtime.env")
    _checked(runtime)
    merged = {**old, **_values(runtime)}
    auth = Path(merged.get("TERMINAL_MCP_AUTH_DATABASE_PATH") or data / "auth.sqlite3")
    main = Path(merged.get("TERMINAL_MCP_DATABASE_PATH") or data / "terminal-mcp.sqlite3")
    username = Path(
        merged.get("TERMINAL_MCP_OAUTH_USERNAME_FILE") or config / "credentials/oauth-username"
    )
    password = Path(
        merged.get("TERMINAL_MCP_OAUTH_PASSWORD_FILE") or config / "credentials/oauth-password"
    )
    if username == password or username in (env, auth, main) or password in (env, auth, main):
        raise ValueError(
            "OAuth credentials must be distinct from each other and from the databases"
        )
    files = (env, runtime, *(Path(str(auth) + s) for s in _SUFFIXES), username, password)
    return merged, auth, main, runtime, tuple(dict.fromkeys(files))


def _private(path: Path) -> None:
    _checked(path)
    if not path.exists():
        return
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError(f"authorization artifact is not owner-only: {path}")


def preflight(config: Path, data: Path) -> str:
    values, auth, _, _, files = paths(config, data)
    for path in files:
        _private(path)
    sidecars = (Path(str(auth) + s) for s in _SUFFIXES[1:])
    if not auth.is_file():
        if any(path.exists() for path in sidecars):
            raise ValueError("auth database is missing but identity/SQLite sidecars remain")
        if any(path.exists() for path in (files[-2], files[-1])):
            raise ValueError(
                "auth database missing but old credential files remain; "
                "recover auth or use --reset-auth"
            )
        return "fresh"
    for suffix in (".access-key", ".identity-anchor"):
        if not Path(str(auth) + suffix).is_file():
            raise ValueError(f"protected auth identity sidecar is missing: {suffix}")
    for label, path in (("username", files[-2]), ("password", files[-1])):
        if not path.is_file() or not path.read_bytes().strip():
            raise ValueError(f"existing authorization is missing OAuth {label} credential file")
    for key in (
        "TERMINAL_MCP_OAUTH_SIGNING_SECRET",
        "TERMINAL_MCP_ADMIN_SESSION_SECRET",
        "TERMINAL_MCP_ADMIN_PASSWORD",
    ):
        if not values.get(key):
            raise ValueError(f"existing authorization is missing {key}")
    # Stdlib-only preflight: install.sh also runs the full lineage assertion
    # with the staged runtime (which contains the required auth dependencies).
    with sqlite3.connect(f"{auth.as_uri()}?mode=ro", uri=True, timeout=5) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("protected authorization SQLite integrity check failed")
        required = {
            "auth_security_state",
            "oauth_identity_migration",
            "oauth_clients",
            "console_devices",
        }
        existing = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not required.issubset(existing):
            raise ValueError("protected authorization foundation/identity marker is incomplete")
    for key in AUTH_KEYS:
        if key in os.environ and key in values and os.environ[key] != values[key]:
            raise ValueError(f"clean-deploy would rotate or change retained authorization: {key}")
    if any(k in os.environ for k in ("TERMINAL_MCP_OAUTH_USERNAME", "TERMINAL_MCP_OAUTH_PASSWORD")):
        raise ValueError(
            "clean-deploy cannot change retained OAuth credential files without --reset-auth"
        )
    return "existing"


def merge_env(config: Path, data: Path, fresh_env: Path) -> None:
    """Add retained auth keys to a fresh runtime config; preserve the old env on failure."""
    merged, _, _, _, _ = paths(config, data)
    _checked(fresh_env)
    _private(fresh_env)
    new = _values(fresh_env)
    for key in AUTH_KEYS:
        if key in merged:
            new[key] = merged[key]
    # Recompose from bounded parsed values, never source shell content.
    fresh_env.write_text(
        "".join(f"{k}={json.dumps(v)}\n" for k, v in new.items()), encoding="utf-8"
    )
    os.chmod(fresh_env, 0o600)
    # The caller atomically renames the fresh environment, then prunes runtime config.


def reset_pairing(auth: Path) -> None:
    """Discard Console device OAuth grants and pending pairing secrets, retaining ChatGPT OAuth."""
    _checked(auth)
    if not auth.exists():
        return  # --reset-auth starts a new empty store during activation.
    with sqlite3.connect(auth, timeout=10) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "DELETE FROM oauth_refresh_tokens WHERE client_id IN "
            "(SELECT client_id FROM console_devices)"
        )
        db.execute(
            "DELETE FROM oauth_codes WHERE client_id IN (SELECT client_id FROM console_devices)"
        )
        db.execute(
            "DELETE FROM oauth_clients WHERE client_id IN (SELECT client_id FROM console_devices)"
        )
        for table in ("console_devices", "console_pairings", "console_pairing_audit"):
            db.execute(f"DELETE FROM {table}")
        db.commit()


def check_bootstrap(
    config: Path,
    data: Path,
    username_file: Path,
    password_file: Path,
    protected: tuple[Path, ...],
) -> None:
    old, auth, main, _, _ = paths(config, data)
    old_credentials = {
        Path(old.get("TERMINAL_MCP_OAUTH_USERNAME_FILE") or config / "credentials/oauth-username"),
        Path(old.get("TERMINAL_MCP_OAUTH_PASSWORD_FILE") or config / "credentials/oauth-password"),
    }
    if (
        username_file == password_file
        or username_file in (auth, main)
        or password_file in (auth, main)
    ):
        raise ValueError("new OAuth credential paths overlap")
    for path in (username_file, password_file):
        _checked(path)
        if any(path == p or path.is_relative_to(p) for p in protected):
            raise ValueError("new OAuth credential path overlaps an active source or release")
        if path.exists() and not (path.is_relative_to(config) or path in old_credentials):
            raise ValueError("new OAuth credential file would overwrite unmanaged data")


def bootstrap_credentials(username_file: Path, password_file: Path) -> None:
    credentials = (
        (username_file, os.environ.get("TERMINAL_MCP_OAUTH_USERNAME", "")),
        (password_file, os.environ.get("TERMINAL_MCP_OAUTH_PASSWORD", "")),
    )
    for _, value in credentials:
        if not value.strip() or "\n" in value or "\r" in value:
            raise ValueError("reset/fresh OAuth username and password must be supplied")
    for path, value in credentials:
        _checked(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The clean inventory must have removed old credentials before bootstrap.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(value.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())


def save_link(config: Path, url: str) -> None:
    if len(url) > 4096 or not url.startswith("https://") or "/connect#" not in url:
        raise ValueError("invalid pairing URL")
    path = config / "pairing-url.txt"
    _checked(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write((url + "\n").encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "preflight",
            "merge-env",
            "runtime-path",
            "main-path",
            "auth-path",
            "reset-pairing",
            "bootstrap",
            "check-bootstrap",
            "save-link",
        ),
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--fresh-env", type=Path)
    parser.add_argument("--username-file", type=Path)
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--protect", type=Path, action="append", default=[])
    args = parser.parse_args()
    if args.action == "preflight":
        print(preflight(args.config, args.data))
    elif args.action == "merge-env":
        if args.fresh_env is None:
            parser.error("--fresh-env is required")
        merge_env(args.config, args.data, args.fresh_env)
    elif args.action == "runtime-path":
        print(paths(args.config, args.data)[3])
    elif args.action == "main-path":
        print(paths(args.config, args.data)[2])
    elif args.action == "auth-path":
        print(paths(args.config, args.data)[1])
    elif args.action == "reset-pairing":
        reset_pairing(paths(args.config, args.data)[1])
    elif args.action == "bootstrap":
        if args.username_file is None or args.password_file is None:
            parser.error("--username-file and --password-file are required")
        bootstrap_credentials(args.username_file, args.password_file)
    elif args.action == "check-bootstrap":
        if args.username_file is None or args.password_file is None:
            parser.error("--username-file and --password-file are required")
        check_bootstrap(
            args.config, args.data, args.username_file, args.password_file, tuple(args.protect)
        )
    elif args.action == "save-link":
        save_link(args.config, sys.stdin.read(4097))


if __name__ == "__main__":
    main()
