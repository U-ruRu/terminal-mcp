"""Windows service configuration, sealed initialization and offline release snapshots."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
import shutil
import sqlite3
import subprocess
import sys
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
    ET.SubElement(service, "env", name="TERMINAL_MCP_WINDOWS_SERVICE_ROOT", value=str(root))
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


def configure_browser_origins(root: Path) -> bool:
    """Converge the installed Console origins while retaining operator additions."""
    from terminal_mcp.config import Settings
    from terminal_mcp.deployment._release_common import atomic, require
    from terminal_mcp.http.browser_security import canonical_origin

    env = root / "config/terminal-mcp.env"
    settings = Settings(_env_file=env)
    existing = [
        canonical_origin(value)
        for value in settings.console_allowed_origins.split(",")
        if value.strip()
    ]
    required = ["https://localhost", canonical_origin(settings.console_public_base_url)]
    merged = list(dict.fromkeys([*existing, *required]))
    if existing == merged:
        return False
    original = env.read_bytes()
    text = original.decode("utf-8-sig")
    prefix = "TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS="
    lines = text.splitlines(keepends=True)
    indices = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    require(len(indices) <= 1, "duplicate_console_origins_configuration")
    replacement = prefix + json.dumps(",".join(merged)) + "\n"
    if indices:
        lines[indices[0]] = replacement
    else:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        lines.append(replacement)
    backup = root / "backups" / ("console-origins-" + hashlib.sha256(original).hexdigest() + ".env")
    if not backup.exists():
        atomic(backup, original)
    require(backup.read_bytes() == original, "console_origins_backup_mismatch")
    atomic(env, "".join(lines).encode("utf-8"))
    return True


def snapshot(root: Path, destination: Path):
    """Run after SCM has stopped the service. Preserve identity sidecars and config."""
    from terminal_mcp.config import Settings
    from terminal_mcp.deployment.update import snapshot as snapshot_stores
    from terminal_mcp.platform_windows import secure_path

    settings = Settings(_env_file=root / "config/terminal-mcp.env")
    stores = configured_stores(settings)
    configs = [
        path for directory in (root / "config", root / "network") for path in directory.rglob("*")
    ]
    configs = [path for path in configs if path.is_file()]
    for path in (
        settings.runtime_config_path,
        settings.oauth_username_file,
        settings.oauth_password_file,
        settings.auth_database_path.with_name(settings.auth_database_path.name + ".access-key"),
        settings.auth_database_path.with_name(
            settings.auth_database_path.name + ".identity-anchor"
        ),
        settings.database_path.parent / "node-identity.json",
    ):
        if path is not None and path.is_file() and path not in configs:
            configs.append(path)
    if (root / "service/runtime.json").is_file():
        configs.append(root / "service/runtime.json")
    snapshot_stores({"stores": stores}, destination, configs)
    secure_path(destination, include_current=False)
    shutil.copytree(root / "config", destination / "config")
    signatures = {name: schema(Path(path)) for name, path in stores.items() if Path(path).exists()}
    (destination / "schemas.json").write_text(json.dumps(signatures), encoding="utf-8")


def configured_stores(settings):
    return {
        "runtime": str(settings.database_path),
        "auth": str(settings.auth_database_path),
        "output": str(settings.output_cache_path),
        "control": str(settings.effective_fleet_control_path()),
        "node_meta": str(settings.effective_fleet_node_meta_path()),
        "projection": str(settings.effective_fleet_projection_path()),
    }


def check_idle(root: Path):
    from terminal_mcp.config import Settings
    from terminal_mcp.deployment._release_common import readonly, require

    path = Settings(_env_file=root / "config/terminal-mcp.env").database_path
    with closing(readonly(path)) as database:
        count = database.execute(
            "SELECT COUNT(*) FROM commands WHERE status IS NULL OR status NOT IN "
            "('completed','failed','cancelled')"
        ).fetchone()[0]
    require(count == 0, "active_commands_block_restart")


async def migrate(root: Path, backup: Path):
    from terminal_mcp.config import Settings
    from terminal_mcp.deployment._release_common import atomic, encoded, sha256
    from terminal_mcp.deployment.release import migrate_stores

    if (backup / "migration.started.json").exists():
        raise RuntimeError("migration already started; inspect the durable receipt")
    settings = Settings(_env_file=root / "config/terminal-mcp.env")
    atomic(backup / "migration.started.json", encoded({"python": sys.executable}))
    versions = await migrate_stores(
        {"stores": configured_stores(settings)}, backup, settings=settings, access_bridge=True
    )
    atomic(
        backup / "migration.completed.json",
        encoded(
            {
                "format": 1,
                "versions": versions,
                "stores": configured_stores(settings),
                "snapshot_sha256": sha256(backup / "snapshot.json"),
            }
        ),
    )


def check_migration(root: Path, backup: Path):
    from terminal_mcp.config import Settings
    from terminal_mcp.deployment._release_common import read_json, require, schema_versions, sha256

    require((backup / "migration.completed.json").is_file(), "migration_receipt_required")
    settings = Settings(_env_file=root / "config/terminal-mcp.env")
    receipt = read_json(backup / "migration.completed.json")
    stores = configured_stores(settings)
    require(
        receipt.get("format") == 1
        and receipt.get("stores") == stores
        and receipt.get("snapshot_sha256") == sha256(backup / "snapshot.json")
        and receipt.get("versions") == schema_versions(stores),
        "migration_receipt_mismatch",
    )
    manifest = read_json(backup / "snapshot.json")
    for item in manifest["stores"].values():
        before = backup / item["backup"]
        require(sha256(before) == item["sha256"], "backup_tampered")
        # migrate_stores verified old rows before publishing this bound completion receipt.
        # A resumed activation keeps legitimate writes made by the new runtime.
    for path, item in manifest["configs"].items():
        require(sha256(Path(path)) == item["sha256"], "protected_configuration_changed")


def assert_rollback_compatible(root: Path, backup: Path):
    from terminal_mcp.config import Settings

    expected = json.loads((backup / "schemas.json").read_text(encoding="utf-8"))
    if (backup / "snapshot.json").exists():
        stores = configured_stores(Settings(_env_file=root / "config/terminal-mcp.env"))
        actual = {name: schema(Path(path)) for name, path in stores.items() if Path(path).exists()}
    else:
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
            "migrate",
            "check-migration",
            "check-idle",
            "check-rollback",
            "probe",
            "browser-origins",
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
            extra_directories=(root / "logs", root / "work", root / "control"),
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
    elif args.action == "browser-origins":
        configure_browser_origins(root)
    elif args.action == "migrate":
        asyncio.run(migrate(root, args.output))
    elif args.action == "check-migration":
        check_migration(root, args.output)
    elif args.action == "check-idle":
        check_idle(root)
    elif args.action == "check-rollback":
        assert_rollback_compatible(root, args.output)
    else:
        asyncio.run(probe())


if __name__ == "__main__":
    main()
