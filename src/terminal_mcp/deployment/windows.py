"""Windows service configuration, sealed initialization and offline release snapshots."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from contextlib import closing
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit


def service_xml(root: PureWindowsPath, python: PureWindowsPath) -> str:
    root, python = PureWindowsPath(root), PureWindowsPath(python)
    for path in (root, python):
        if not path.is_absolute() or ".." in path.parts or any(c in str(path) for c in '\r\n"%'):
            raise ValueError("service paths require absolute Windows paths")
    if not python.is_relative_to(root / "releases"):
        raise ValueError("service Python must belong to an installed release")
    service = ET.Element("service")
    fields = {
        "id": "TerminalMCP",
        "name": "Terminal MCP",
        "description": "Terminal MCP Windows node",
        "executable": str(python),
        "arguments": subprocess.list2cmdline(
            [
                "-m",
                "terminal_mcp.windows_service",
                "--env-file",
                str(root / "config/terminal-mcp.env"),
            ]
        ),
        "workingdirectory": str(root / "work"),
        "startmode": "Automatic",
        "delayedAutoStart": "true",
        "hidewindow": "true",
        "stoptimeout": "30 sec",
        "logpath": str(root / "logs"),
    }
    for name, value in fields.items():
        ET.SubElement(service, name).text = value
    ET.SubElement(service, "env", name="PYTHONUTF8", value="1")
    ET.SubElement(
        service,
        "env",
        name="TERMINAL_MCP_ENV_FILE_PATH",
        value=str(root / "config/terminal-mcp.env"),
    )
    ET.SubElement(service, "onfailure", action="restart", delay="10 sec")
    ET.SubElement(service, "resetfailure").text = "1 hour"
    ET.SubElement(service, "log", mode="roll-by-size").append(ET.Element("sizeThreshold"))
    service.find("log/sizeThreshold").text = "10240"
    ET.indent(service)
    return ET.tostring(service, encoding="unicode") + "\n"


def validate_public_url(public_url: str) -> None:
    parsed = urlsplit(public_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("public URL requires an HTTPS origin")


def initialize(root: Path, public_url: str, host: str, port: int):
    from terminal_mcp.platform_windows import secure_path

    validate_public_url(public_url)
    env = root / "config/terminal-mcp.env"
    if env.exists():
        return
    for relative in ("", "config", "config/credentials", "data", "logs", "work", "backups"):
        path = root / relative
        path.mkdir(parents=True, exist_ok=True)
        secure_path(path, include_current=False)
    password = secrets.token_urlsafe(32)
    values = {
        "HOST": host,
        "PORT": str(port),
        "PUBLIC_BASE_URL": public_url.rstrip("/"),
        "ENV_FILE_PATH": str(env),
        "RUNTIME_CONFIG_PATH": str(root / "config/runtime.env"),
        "DATABASE_PATH": str(root / "data/terminal-mcp.sqlite3"),
        "AUTH_DATABASE_PATH": str(root / "data/auth.sqlite3"),
        "OUTPUT_CACHE_PATH": str(root / "data/output.sqlite3"),
        "LOG_PATH": str(root / "logs/terminal-mcp.log"),
        "CWD": str(root / "work"),
        "TERMINAL_USER": "current",
        "EXECUTION_MODE": "in_process",
        "AUTH_MODE": "oauth",
        "MCP_AUTH_MODE": "oauth",
        "ACTIONS_AUTH_MODE": "oauth",
        "ADMIN_USERNAME": "owner",
        "ADMIN_PASSWORD": password,
        "ADMIN_SESSION_SECRET": secrets.token_urlsafe(48),
        "OAUTH_SIGNING_SECRET": secrets.token_urlsafe(48),
        "OAUTH_USERNAME_FILE": str(root / "config/credentials/oauth-username"),
        "OAUTH_PASSWORD_FILE": str(root / "config/credentials/oauth-password"),
        "PERSISTENT_AGENTS_ENABLED": "true",
        "ACCESS_MESH_ENABLED": "true",
        "FLEET_INSTANCE_ID": "windows-laptop",
        "FLEET_PEERS_JSON": "[]",
        "ACCESS_MESH_PEERS_JSON": "[]",
        "ACCESS_MESH_PROOF_KEY": secrets.token_urlsafe(48),
        "FLEET_SIGNING_PRIVATE_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
    }
    for name, value in (("oauth-username", "owner"), ("oauth-password", password)):
        with (root / "config/credentials" / name).open("x", encoding="utf-8") as handle:
            handle.write(value + "\n")
    with env.open("x", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"TERMINAL_MCP_{key}={json.dumps(value)}\n")


def schema(path: Path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
        rows = database.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
        ).fetchall()
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def snapshot(root: Path, destination: Path):
    """Run after SCM has stopped the service. Preserve identity sidecars and config."""
    from terminal_mcp.platform_windows import secure_path

    destination.mkdir(parents=True, exist_ok=False)
    secure_path(destination, include_current=False)
    shutil.copytree(root / "config", destination / "config")
    data = destination / "data"
    data.mkdir()
    signatures = {}
    for path in (root / "data").iterdir():
        if path.name.endswith(("-wal", "-shm", "-journal")):
            continue
        if path.suffix == ".sqlite3":
            with closing(sqlite3.connect(path)) as source:
                with closing(sqlite3.connect(data / path.name)) as target:
                    source.backup(target)
                    if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError("backup integrity check failed")
            signatures[path.name] = schema(path)
        elif path.is_file():
            shutil.copy2(path, data / path.name)
    (destination / "schemas.json").write_text(json.dumps(signatures), encoding="utf-8")


def assert_rollback_compatible(root: Path, backup: Path):
    expected = json.loads((backup / "schemas.json").read_text(encoding="utf-8"))
    actual = {path.name: schema(path) for path in (root / "data").glob("*.sqlite3")}
    if expected != actual:
        raise RuntimeError("code rollback requires matching durable database schemas")


async def probe():
    from terminal_mcp.app import create_app
    from terminal_mcp.config import Settings

    with tempfile.TemporaryDirectory(prefix="tmcp-windows-stage-") as temporary:
        path = Path(temporary)
        app = create_app(
            Settings(
                _env_file=None,
                cwd=path,
                database_path=path / "runtime.sqlite3",
                auth_database_path=path / "auth.sqlite3",
                output_cache_path=path / "output.sqlite3",
                runtime_config_path=path / "runtime.env",
                env_file_path=path / "server.env",
                log_path=path / "events.log",
                metrics_port=0,
            )
        )
        async with app.router.lifespan_context(app):
            result = await app.state.service.terminal.capture("Write-Output stage-ready")
            if not result["ok"] or not any("stage-ready" in line for line in result["lines"]):
                raise RuntimeError("Windows staging command failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "initialize",
            "clean",
            "check-install",
            "render",
            "backup",
            "check-rollback",
            "probe",
        ),
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--public-url")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "initialize":
        initialize(root, args.public_url, args.host, args.port)
    elif args.action in {"clean", "check-install"}:
        from terminal_mcp.deployment.clean import inventory

        validate_public_url(args.public_url)
        state = inventory(
            root=root,
            config=root / "config",
            data=root / "data",
            cache=root / "cache",
            backups=root / "backups",
            extra_directories=(root / "logs", root / "work"),
        )
        state.validate(protected=(Path(__file__).absolute(),))
        if args.action == "check-install":
            state.require_empty()
        elif args.apply:
            state.apply(protected=(Path(__file__).absolute(),))
    elif args.action == "render":
        args.output.write_text(service_xml(root, args.python), encoding="utf-8")
    elif args.action == "backup":
        snapshot(root, args.output)
    elif args.action == "check-rollback":
        assert_rollback_compatible(root, args.output)
    else:
        asyncio.run(probe())


if __name__ == "__main__":
    main()
