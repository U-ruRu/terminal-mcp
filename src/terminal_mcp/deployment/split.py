"""Split-service preparation and read-only cutover safety checks.

No database is copied, migrated, restored or opened read-write here. The default
CLI only renders a plan. Activation is deliberately separate from preparation.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from terminal_mcp.config import Settings

API_SERVICE = "terminal-mcp.service"
EXECUTOR_SERVICE = "terminal-mcp-executor.service"
MIN_FREE_BYTES = 64 * 1024 * 1024


class CutoverError(RuntimeError):
    pass


def _unit_value(value: str | Path) -> str:
    """Reject systemd interpolation, control characters and ambiguous paths."""
    text = str(value)
    if not text or any(char.isspace() or char in '%$"\\\x00' for char in text):
        raise CutoverError("unsafe_systemd_value")
    return text


def _absolute(path: Path) -> str:
    text = _unit_value(path)
    if not path.is_absolute() or ".." in path.parts:
        raise CutoverError("absolute_path_required")
    return text


@dataclass(frozen=True)
class SplitServiceSpec:
    api_user: str
    api_uid: int
    api_group: str = "terminal-mcp"
    install_root: Path = Path("/opt/terminal-mcp")
    env_dir: Path = Path("/etc/terminal-mcp")
    unit_dir: Path = Path("/etc/systemd/system")

    def __post_init__(self):
        for name in (self.api_user, self.api_group):
            if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", name) or name == "root":
                raise CutoverError("non_root_api_identity_required")
        if type(self.api_uid) is not int or self.api_uid <= 0:
            raise CutoverError("non_root_api_uid_required")
        for path in (self.install_root, self.env_dir, self.unit_dir):
            _absolute(path)

    def render(self, settings: Settings) -> dict[Path, str]:
        # One root-owned runtime directory. API may connect but cannot replace
        # the socket; SO_PEERCRED is the second, independent caller restriction.
        if settings.executor_socket_path != Path("/run/terminal-mcp/executor.sock"):
            raise CutoverError("managed_topology_requires_standard_socket")
        shell = _absolute(Path(settings.shell))
        cwd = _absolute(settings.cwd)
        user = _unit_value(settings.terminal_user)
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", user):
            raise CutoverError("invalid_terminal_user")
        python = self.install_root / "current/bin/python"
        mode_file = self.env_dir.with_name(self.env_dir.name + "-executor") / "client.env"
        exec_start = (
            f"{python} -m terminal_mcp.terminal.executor_service "
            f"--socket /run/terminal-mcp/executor.sock --allowed-uid {self.api_uid} "
            f"--shell {shell} --cwd {cwd} --user {user} --grace {settings.cancel_grace_sec}"
        )
        executor = f"""[Unit]
Description=Terminal MCP local privileged executor
After=local-fs.target
[Service]
Type=simple
User=root
Group={self.api_group}
WorkingDirectory=/
RuntimeDirectory=terminal-mcp
RuntimeDirectoryMode=0750
RuntimeDirectoryPreserve=restart
UMask=0077
ExecStart={exec_start}
KillMode=control-group
TimeoutStopSec=15
Restart=on-failure
RestartSec=1
[Install]
WantedBy=multi-user.target
"""
        # Wants (not PartOf/BindsTo) keeps executor lifetime independent of API.
        # A second EnvironmentFile wins over the original instance env file.
        api = f"""[Unit]
Wants={EXECUTOR_SERVICE}
After={EXECUTOR_SERVICE}
[Service]
User={self.api_user}
Group={self.api_group}
NoNewPrivileges=true
CapabilityBoundingSet=
RestrictSUIDSGID=true
UMask=0077
EnvironmentFile={mode_file}
"""
        mode = (
            'TERMINAL_MCP_EXECUTION_MODE="unix"\n'
            'TERMINAL_MCP_EXECUTOR_SOCKET_PATH="/run/terminal-mcp/executor.sock"\n'
            f'TERMINAL_MCP_EXECUTOR_RPC_TIMEOUT_SEC="{settings.executor_rpc_timeout_sec}"\n'
        )
        return {
            self.unit_dir / EXECUTOR_SERVICE: executor,
            mode_file: mode,
        } | {self.unit_dir / f"{API_SERVICE}.d/50-local-executor.conf": api}


def rendered_files(spec: SplitServiceSpec, settings: Settings) -> dict[Path, str]:
    # No mutation of the existing main unit: rollback only removes our drop-in.
    return spec.render(settings)


def _readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise CutoverError(f"required_store_missing:{path}")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    db.execute("PRAGMA query_only=ON")
    return db


def assert_drained(path: Path) -> None:
    try:
        with _readonly(path) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM commands WHERE status NOT IN "
                "('completed', 'failed', 'cancelled')"
            ).fetchone()[0]
            if count:
                raise CutoverError(f"commands_not_drained:{count}")
    except sqlite3.Error as exc:
        raise CutoverError("runtime_store_unreadable") from exc
    finally:
        if "db" in locals():
            db.close()


def preflight(settings: Settings, *, reserve_bytes: int = MIN_FREE_BYTES) -> dict:
    """Run before stopping admission AND again after API is confirmed stopped."""
    if reserve_bytes < MIN_FREE_BYTES:
        raise CutoverError("disk_reserve_too_small")
    assert_drained(settings.database_path)
    required = [settings.database_path, settings.auth_database_path]
    optional = [
        settings.output_cache_path,
        settings.effective_fleet_node_meta_path(),
        settings.effective_fleet_control_path(),
        settings.effective_fleet_projection_path(),
    ]
    if settings.fleet_v1_authority_enabled:
        required.append(settings.effective_fleet_control_path())
    checked = []
    for path in dict.fromkeys(required + [p for p in optional if p.exists()]):
        try:
            with _readonly(path) as db:
                rows = db.execute("PRAGMA quick_check").fetchmany(2)
                if rows != [("ok",)]:
                    raise CutoverError(f"store_integrity_failed:{path}")
        except sqlite3.Error as exc:
            raise CutoverError(f"store_unreadable:{path}") from exc
        finally:
            if "db" in locals():
                db.close()
        free = shutil.disk_usage(path.parent).free
        if free < reserve_bytes:
            raise CutoverError(f"insufficient_disk_space:{path.parent}")
        checked.append({"path": str(path), "integrity": "ok", "free_bytes": free})
    return {"drained": True, "stores": checked, "database_backups_created": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("render", "check"))
    parser.add_argument("--env-file", type=Path, default=Path("/etc/terminal-mcp/terminal-mcp.env"))
    parser.add_argument("--api-user", default="terminal-mcp")
    parser.add_argument("--api-uid", type=int)
    parser.add_argument("--api-group", default="terminal-mcp")
    parser.add_argument("--install-root", type=Path, default=Path("/opt/terminal-mcp"))
    parser.add_argument("--unit-dir", type=Path, default=Path("/etc/systemd/system"))
    args = parser.parse_args()
    if not args.env_file.is_file():
        parser.error("the existing instance env file is required")
    settings = Settings(_env_file=args.env_file)
    if args.action == "check":
        result = preflight(settings)
    else:
        if args.api_uid is None:
            import pwd

            args.api_uid = pwd.getpwnam(args.api_user).pw_uid
        spec = SplitServiceSpec(
            args.api_user,
            args.api_uid,
            args.api_group,
            args.install_root,
            args.env_file.parent,
            args.unit_dir,
        )
        result = {str(path): text for path, text in rendered_files(spec, settings).items()}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
