"""Explicit operator driver for same-release, drained split-service cutover.

Never creates users, changes database ownership, restores databases or switches
release links. Dedicated API paths must be provisioned before activation.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

from terminal_mcp.config import Settings
from terminal_mcp.deployment.split import (
    API_SERVICE,
    EXECUTOR_SERVICE,
    CutoverError,
    SplitServiceSpec,
    preflight,
    rendered_files,
)
from terminal_mcp.deployment.transition import TopologyTransition


def systemctl(*args: str) -> None:
    subprocess.run(["systemctl", *args], check=True, timeout=30, capture_output=True)


def check_root_controlled_paths(paths: list[Path]) -> None:
    # Ancestors may not be API-writable: otherwise the API could replace the
    # client-mode file, immutable release, journal or root-executor unit.
    for path in paths:
        for ancestor in [path, *path.parents]:
            if ancestor.is_symlink():
                raise CutoverError("symlink_privileged_path")
            if ancestor.exists():
                stat = ancestor.stat()
                if stat.st_uid != 0 or stat.st_mode & 0o022:
                    raise CutoverError(f"unsafe_privileged_path:{ancestor}")


def check_api_permissions(settings: Settings, spec: SplitServiceSpec) -> None:
    paths = {
        settings.database_path,
        settings.auth_database_path,
        settings.output_cache_path,
        settings.runtime_config_path,
        settings.log_path,
        settings.env_file_path,
        settings.effective_fleet_node_meta_path(),
        settings.effective_fleet_control_path(),
        settings.effective_fleet_projection_path(),
    }
    script = (
        "import json,os,sys; from pathlib import Path; "
        "paths=[Path(s) for s in json.loads(sys.argv[1])]; "
        "bad=[str(p) for p in paths if not os.access(p.parent,os.R_OK|os.W_OK|os.X_OK) "
        "or (p.exists() and not os.access(p,os.R_OK|os.W_OK))]; "
        "print(json.dumps(bad)); sys.exit(bool(bad))"
    )
    result = subprocess.run(
        [
            "runuser",
            "-u",
            spec.api_user,
            "--",
            str(spec.install_root / "current/bin/python"),
            "-c",
            script,
            json.dumps(sorted(map(str, paths))),
        ],
        check=False,
        timeout=15,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise CutoverError("api_paths_not_provisioned:" + result.stdout.strip()[:2000])


def _executor_ready(settings: Settings, spec: SplitServiceSpec) -> None:
    # Probe as the allowlisted API UID, before API starts and while drained.
    script = """import asyncio,sys
from terminal_mcp.terminal.ipc import UnixExecutionAdapter
async def probe():
    client = UnixExecutionAdapter(sys.argv[1], rpc_timeout=2)
    try:
        await client.start()
    finally:
        await client.close()
asyncio.run(probe())
"""
    subprocess.run(
        [
            "runuser",
            "-u",
            spec.api_user,
            "--",
            str(spec.install_root / "current/bin/python"),
            "-c",
            script,
            str(settings.executor_socket_path),
        ],
        check=True,
        timeout=5,
        capture_output=True,
    )


def readiness(settings: Settings, spec: SplitServiceSpec, health_url: str, unit: str) -> None:
    deadline = time.monotonic() + 30
    last = None
    while time.monotonic() < deadline:
        try:
            systemctl("is-active", "--quiet", unit)
            if unit == EXECUTOR_SERVICE:
                _executor_ready(settings, spec)
            elif unit == API_SERVICE:
                with urlopen(health_url, timeout=2) as response:
                    if response.status != 200:
                        raise CutoverError("api_health_not_ready")
            else:
                raise CutoverError("unknown_service")
            return
        except (OSError, RuntimeError, subprocess.SubprocessError, TimeoutError) as exc:
            last = exc
            time.sleep(0.2)
    raise CutoverError(f"service_readiness_failed:{unit}") from last


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("activate", "rollback"))
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--api-user", default="terminal-mcp")
    parser.add_argument("--api-group", default="terminal-mcp")
    parser.add_argument("--install-root", type=Path, default=Path("/opt/terminal-mcp"))
    parser.add_argument("--unit-dir", type=Path, default=Path("/etc/systemd/system"))
    parser.add_argument(
        "--journal", type=Path, default=Path("/var/lib/terminal-mcp-topology/transition.json")
    )
    parser.add_argument("--health-url", default="http://127.0.0.1:8080/health/live")
    parser.add_argument(
        "--approved-gates",
        action="store_true",
        help="operator confirms Architecture C and P0 integrity gates accepted",
    )
    args = parser.parse_args()
    if os.geteuid() != 0 or not args.approved_gates:
        parser.error("root and explicit --approved-gates are required")
    if not args.env_file.is_file():
        parser.error("existing instance env file is required")
    identity = pwd.getpwnam(args.api_user)
    spec = SplitServiceSpec(
        args.api_user,
        identity.pw_uid,
        args.api_group,
        args.install_root,
        args.env_file.parent,
        args.unit_dir,
    )
    settings = Settings(_env_file=args.env_file, env_file_path=args.env_file)
    immutable_release = (args.install_root / "current").resolve(strict=True)
    files = rendered_files(spec, settings)
    check_root_controlled_paths([*files, args.journal, immutable_release])
    if args.action == "activate":
        from terminal_mcp.terminal.ipc import UnixExecutionAdapter

        assert UnixExecutionAdapter is not None
        if settings.execution_mode != "in_process":
            parser.error("initial migration requires explicit in_process base configuration")
        check_api_permissions(settings, spec)
    transition = TopologyTransition(
        files,
        args.journal,
        control=systemctl,
        check=lambda: preflight(settings),
        ready=lambda unit: readiness(settings, spec, args.health_url, unit),
        identity=str((args.install_root / "current").resolve(strict=True)),
    )
    getattr(transition, args.action)()
    print(json.dumps({"ok": True, "action": args.action, "journal": str(args.journal)}))


if __name__ == "__main__":
    main()
