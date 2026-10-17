"""Offline-backed, journaled release switching. Never restores a database automatically.

This manages an already-provisioned node; joining Mesh, changing TLS/authentication
and provisioning secrets are separate operations. A coordinator-owned idle window
is mandatory. The services are checked before and after stopping admission.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from terminal_mcp.deployment._release_common import (
    SHA,
    ReleaseError,
    atomic,
    encoded,
    read_json,
    readonly,
    require,
    run,
    safe_path,
    schema_versions,
    sha256,
    sync_dir,
    utc,
)
from terminal_mcp.deployment.artifact import (
    clean_env,
    platform_id,
    runtime_command,
    unpack,
    verify_approval,
)
from terminal_mcp.deployment.probes import ProbeClient, checked_url

BEFORE_MIGRATION = {
    "prepared",
    "staging",
    "staged",
    "stopping",
    "stopped",
    "backing_up",
    "backed_up",
}
FORWARD_PHASES = {"migrated", "switching", "switched", "starting", "verifying"}
PHASES = BEFORE_MIGRATION | FORWARD_PHASES | {"migrating", "applied", "aborted"}
# Protect every existing state table except disposable output and append-only logs.
VOLATILE_PREFIXES = ("command_output", "output_", "event_log", "observability_", "sqlite_")


def env_values(path: Path) -> dict[str, str]:
    values = {}
    require(path.is_file() and path.stat().st_size < 1024 * 1024, "existing_env_required")
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, separator, value = line.partition("=")
        require(separator and re.fullmatch(r"[A-Z][A-Z0-9_]*", key), "unsupported_env_syntax")
        try:
            tokens = shlex.split(value, comments=True)
        except ValueError as exc:
            raise ReleaseError("unsupported_env_syntax") from exc
        require(len(tokens) <= 1, "unsupported_env_syntax")
        values[key] = tokens[0] if tokens else ""
    return values


def unit_environment(profile: dict) -> tuple[dict[str, str], list[Path]]:
    """Read literal systemd service environment without evaluating shell commands.

    EnvironmentFile values override Environment values independent of directive order.
    Unsupported substitutions/continuations fail before any service mutation.
    """
    direct: dict[str, str] = {}
    files: list[Path] = []
    api_files = [
        Path(p)
        for p in profile["unit_files"]
        if Path(p).name == "terminal-mcp.service" or "terminal-mcp.service.d" in Path(p).parts
    ]
    require(api_files, "api_unit_inventory_missing")
    for unit in api_files:
        safe_path(unit, owner=True)
        require(unit.is_file(), "existing_service_units_required")
        section = ""
        for raw in unit.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("["):
                section = line
                continue
            if section != "[Service]":
                continue
            key, sep, value = line.partition("=")
            if not sep or key not in {
                "Environment",
                "EnvironmentFile",
                "PassEnvironment",
                "UnsetEnvironment",
                "ExecStart",
            }:
                continue
            require(not value.endswith(chr(92)), "unit_continuation_requires_review")
            if key in {"PassEnvironment", "UnsetEnvironment"}:
                require(not value.strip(), "inherited_unit_environment_requires_review")
                continue
            if key == "ExecStart":
                if value:
                    words = shlex.split(value)
                    expected = str(Path(profile["root"]) / "current/bin/terminal-mcp")
                    require(words == [expected], "unmanaged_api_launcher_requires_review")
                continue
            words = shlex.split(value)
            if key == "Environment":
                if not words:
                    direct.clear()
                for word in words:
                    name, sep, text = word.partition("=")
                    require(
                        sep and re.fullmatch(r"[A-Z][A-Z0-9_]*", name) and "%" not in text,
                        "unsupported_unit_environment",
                    )
                    direct[name] = text
            else:
                if not words:
                    files.clear()
                for word in words:
                    optional = word.startswith("-")
                    filename = word[1:] if optional else word
                    require(
                        not any(c in filename for c in "*?[]%$"),
                        "expanded_env_file_requires_review",
                    )
                    path = safe_path(Path(filename), owner=True)
                    if optional and not path.exists():
                        continue
                    require(path.is_file(), "configured_env_file_missing")
                    files.append(path)
    require(Path(profile["env_file"]) in files, "main_env_not_used_by_service")
    for path in files:
        direct.update(env_values(path))
    return direct, files


def installed_unit_files(services: list[str]) -> list[Path]:
    """Only filenames; neither systemd Environment nor credentials enter output."""
    paths = []
    for unit in services:
        fragment = run(
            ["systemctl", "show", "--property=FragmentPath", "--value", unit],
            code="unit_inventory",
            timeout=10,
        ).strip()
        drops = run(
            ["systemctl", "show", "--property=DropInPaths", "--value", unit],
            code="unit_inventory",
            timeout=10,
        ).strip()
        require(fragment, "service_not_provisioned")
        paths.extend(Path(p) for p in [fragment, *shlex.split(drops)])
    return paths


def truth(value: str) -> bool:
    return value.lower() in ("true", "yes", "1", "on")


def configured_stores(values: dict[str, str]) -> dict[str, str]:
    runtime = values.get("TERMINAL_MCP_DATABASE_PATH", "")
    auth = values.get("TERMINAL_MCP_AUTH_DATABASE_PATH", "")
    output = values.get("TERMINAL_MCP_OUTPUT_CACHE_PATH", "")
    require(
        all(Path(p).is_absolute() for p in (runtime, auth, output)),
        "explicit_absolute_store_paths_required",
    )
    parent = Path(runtime).parent
    return {
        "runtime": runtime,
        "auth": auth,
        "output": output,
        "control": values.get("TERMINAL_MCP_FLEET_CONTROL_PATH")
        or str(parent / "fleet-control.sqlite3"),
        "node_meta": values.get("TERMINAL_MCP_FLEET_NODE_META_PATH")
        or str(parent / "fleet-node-meta.sqlite3"),
        "projection": values.get("TERMINAL_MCP_FLEET_PROJECTION_PATH")
        or str(parent / "fleet-projection.sqlite3"),
    }


def mesh_peers(values: dict[str, str], stores: dict[str, str]) -> dict[str, str]:
    local = values.get("TERMINAL_MCP_FLEET_INSTANCE_ID", "")
    peers = {}
    try:
        configured = json.loads(values.get("TERMINAL_MCP_FLEET_PEERS_JSON", "[]"))
        require(isinstance(configured, list), "unsupported_mesh_peer_configuration")
        for p in configured:
            identity = p.get("instance_id") or p.get("node_id")
            origin = p.get("base_url") or p.get("origin")
            require(identity and origin, "mesh_peer_origin_required")
            peers[identity] = checked_url(origin) + "/health/live"
        mesh_ids = set(json.loads(values.get("TERMINAL_MCP_ACCESS_MESH_PEERS_JSON", "[]")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReleaseError("unsupported_mesh_peer_configuration") from exc
    control = Path(stores["control"])
    if control.exists():
        db = readonly(control)
        try:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "managed_nodes" in tables:
                for identity, origin in db.execute(
                    "SELECT node_id,origin FROM managed_nodes WHERE state != 'detached'"
                ):
                    if identity != local:
                        require(origin, "managed_peer_origin_required")
                        peers[identity] = checked_url(origin) + "/health/live"
            if "fleet_members" in tables:
                mesh_ids.update(r[0] for r in db.execute("SELECT node_id FROM fleet_members"))
        finally:
            db.close()
    mesh_ids.discard(local)
    peers.pop(local, None)
    require(mesh_ids <= peers.keys(), "incomplete_mesh_inventory")
    return peers


def config_inventory(profile: dict, values: dict[str, str], stores: dict[str, str]) -> list[Path]:
    env = Path(profile["env_file"])
    paths = [env.parent, Path(profile["caddyfile"]), *map(Path, profile["unit_files"])]
    # Defaults remain at their existing locations. No key is copied into a release directory.
    for key, default in (
        ("TERMINAL_MCP_RUNTIME_CONFIG_PATH", str(env.parent / "runtime.env")),
        ("TERMINAL_MCP_FLEET_PEER_TRANSPORTS_PATH", str(env.parent / "fleet-peer-transports.json")),
        ("TERMINAL_MCP_MESH_VPN_STATE_DIR", str(env.parent / "mesh-vpn")),
    ):
        paths.append(Path(values.get(key) or default))
    for suffix in (".access-key", ".identity-anchor"):
        paths.append(Path(stores["auth"] + suffix))
    paths += [Path(p) for p in profile.get("extra_config_paths", [])]
    _, unit_env_files = unit_environment(profile)
    paths += unit_env_files
    files: set[Path] = set()
    for p in paths:
        safe_path(p)
        if not p.exists():
            continue
        if p.is_dir():
            for directory, dirs, names in os.walk(p, followlinks=False):
                require(len(files) + len(dirs) + len(names) <= 2048, "config_inventory_too_large")
                for name in dirs:
                    safe_path(Path(directory) / name)
                for name in names:
                    q = safe_path(Path(directory) / name)
                    require(q.is_file(), "special_configuration_file")
                    files.add(q)
        else:
            require(p.is_file(), "special_configuration_file")
            files.add(p)
    require(
        len(files) <= 2048 and sum(p.stat().st_size for p in files) <= 64 * 1024**2,
        "config_inventory_too_large",
    )
    return sorted(files)


def create_profile(
    env: Path, target: str, root: Path, caddyfile: Path, unit_files: list[Path]
) -> dict:
    values, env_files = unit_environment(
        {"env_file": str(env), "root": str(root), "unit_files": list(map(str, unit_files))}
    )
    stores = configured_stores(values)
    unix = values.get("TERMINAL_MCP_EXECUTION_MODE") == "unix"
    return {
        "format": 1,
        "target": target,
        "hostname": socket.gethostname(),
        "root": str(root),
        "env_file": str(env),
        "caddyfile": str(caddyfile),
        "unit_files": list(map(str, unit_files)),
        "stores": stores,
        "services": (["terminal-mcp-executor.service"] if unix else []) + ["terminal-mcp.service"],
        "health_url": "http://127.0.0.1:"
        + values.get("TERMINAL_MCP_PORT", "8080")
        + "/health/live",
        "public_base_url": values.get("TERMINAL_MCP_PUBLIC_BASE_URL", ""),
        "peers": mesh_peers(values, stores),
        "extra_config_paths": [],
        "reserve_bytes": 256 * 1024**2,
        "ready_timeout": 60,
    }


def _table_rows(db: sqlite3.Connection, table: str, columns: list[str]) -> Counter:
    def quote(text: str) -> str:
        return '"' + text.replace('"', '""') + '"'

    rows: Counter = Counter()
    sql = "SELECT " + ",".join(map(quote, columns)) + " FROM " + quote(table)
    for row in db.execute(sql):
        # Hash only; raw credentials/identity material never enter reports or journals.
        body = json.dumps(
            list(row),
            ensure_ascii=True,
            separators=(",", ":"),
            default=lambda b: {"bytes": bytes(b).hex()},
        ).encode()
        rows[hashlib.sha256(body).hexdigest()] += 1
    return rows


def preserved_rows(before: Path, after: Path) -> None:
    """All old rows/columns must survive; migrations may add columns and rows."""
    a, b = readonly(before), readonly(after)
    try:
        tables = [r[0] for r in a.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            if table.startswith(VOLATILE_PREFIXES):
                continue
            quoted = table.replace('"', '""')
            columns = [r[1] for r in a.execute(f'PRAGMA table_info("{quoted}")')]
            current = {r[1] for r in b.execute(f'PRAGMA table_info("{quoted}")')}
            require(set(columns) <= current, "protected_state_columns_removed")
            require(
                not (_table_rows(a, table, columns) - _table_rows(b, table, columns)),
                "protected_state_rows_changed",
            )
    finally:
        a.close()
        b.close()


def snapshot(profile: dict, destination: Path, configs: list[Path]) -> dict:
    require(not destination.exists(), "backup_already_exists")
    destination.mkdir(parents=True, mode=0o700)
    manifest = {"format": 1, "created_at": utc(), "stores": {}, "configs": {}}
    for name, path in profile["stores"].items():
        source = Path(path)
        if not source.exists():
            continue
        target = destination / (name + ".sqlite3")
        db, copy = readonly(source), sqlite3.connect(target)
        try:
            db.backup(copy)
            require(
                copy.execute("PRAGMA quick_check").fetchall() == [("ok",)],
                "backup_integrity_failed",
            )
        finally:
            copy.close()
            db.close()
        target.chmod(0o600)
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
        manifest["stores"][name] = {"path": path, "backup": target.name, "sha256": sha256(target)}
    for index, source in enumerate(configs):
        target = destination / f"config-{index:04d}"
        atomic(target, source.read_bytes())
        info = source.stat()
        manifest["configs"][str(source)] = {
            "backup": target.name,
            "sha256": sha256(source),
            "mode": info.st_mode & 0o777,
            "uid": info.st_uid,
            "gid": info.st_gid,
        }
    atomic(destination / "snapshot.json", encoded(manifest))
    return manifest


class NodeDriver:
    """Concrete Linux adapter; the transaction below is also tested with isolated adapters."""

    def __init__(self, profile_path: Path, target: str):
        self.profile_path = safe_path(profile_path, owner=True)
        self.p = read_json(profile_path)
        require(
            self.p.get("format") == 1 and self.p.get("target") == target, "target_profile_mismatch"
        )
        require(self.p.get("hostname") == socket.gethostname(), "target_hostname_mismatch")
        self.root = safe_path(Path(self.p["root"]), owner=True)
        self.stores = self.p["stores"]
        self.values, self.env_files = unit_environment(self.p)
        require(self.stores == configured_stores(self.values), "store_configuration_drift")
        require(
            self.p.get("peers", {}) == mesh_peers(self.values, self.stores), "mesh_inventory_drift"
        )
        self.services = self.p["services"]
        require(
            self.services
            in (
                ["terminal-mcp.service"],
                ["terminal-mcp-executor.service", "terminal-mcp.service"],
            ),
            "unsupported_services",
        )
        require(
            self.values.get("TERMINAL_MCP_MCP_AUTH_MODE") == "oauth", "configured_oauth_required"
        )
        require(
            self.values.get("TERMINAL_MCP_ACTIONS_AUTH_MODE") == "bearer",
            "configured_management_auth_required",
        )
        expected_services = (
            ["terminal-mcp-executor.service"]
            if self.values.get("TERMINAL_MCP_EXECUTION_MODE") == "unix"
            else []
        )
        expected_services += ["terminal-mcp.service"]
        require(self.services == expected_services, "execution_topology_profile_mismatch")
        self.configs = config_inventory(self.p, self.values, self.stores)
        self.client = ProbeClient()

    def close(self) -> None:
        self.client.close()

    def current(self) -> Path | None:
        current = self.root / "current"
        if not current.exists() and not current.is_symlink():
            return None
        require(current.is_symlink(), "current_must_be_release_symlink")
        resolved = current.resolve(strict=True)
        require(resolved.parent == self.root / "releases", "current_outside_release_root")
        return resolved

    def control(self, verb: str, unit: str) -> None:
        run(["systemctl", verb, unit], timeout=45, code="service_" + verb)

    def active(self, unit: str) -> bool:
        status = run(
            ["systemctl", "show", "--property=ActiveState", "--value", unit],
            timeout=10,
            code="service_observation",
        ).strip()
        require(status in {"active", "inactive", "failed"}, "service_transition_unresolved")
        return status == "active"

    def drained(self) -> None:
        path = Path(self.stores["runtime"])
        if not path.exists():
            return
        db = readonly(path)
        try:
            count = db.execute(
                "SELECT COUNT(*) FROM commands WHERE status IS NULL OR status NOT IN "
                "('completed','failed','cancelled')"
            ).fetchone()[0]
            require(count == 0, "active_commands_block_restart")
        except sqlite3.Error as exc:
            raise ReleaseError("command_state_unreadable") from exc
        finally:
            db.close()

    def admission_guard(self, source_sha: str) -> None:
        # Split mode preserves executor work if admission races with API shutdown.
        # In-process nodes need a coordinator-established pause of every producer.
        if len(self.services) == 2:
            return
        name = self.p.get("admission_guard_file")
        require(isinstance(name, str) and name, "in_process_requires_coordinator_admission_pause")
        path = safe_path(Path(name), owner=True)
        guard = read_json(path)
        require(
            guard.get("target") == self.p["target"]
            and guard.get("source_sha") == source_sha
            and guard.get("command_producers_paused") is True
            and isinstance(guard.get("approved_by"), str)
            and guard["approved_by"],
            "coordinator_admission_pause_mismatch",
        )
        try:
            start = datetime.fromisoformat(guard["not_before"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(guard["expires_at"].replace("Z", "+00:00"))
            now = datetime.now(UTC)
            require(
                start.tzinfo is not None
                and end.tzinfo is not None
                and start <= now < end
                and 0 < (end - start).total_seconds() <= 3600,
                "coordinator_admission_pause_expired",
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReleaseError("coordinator_admission_pause_invalid") from exc

    def schemas(self) -> dict:
        return schema_versions(self.stores)

    def preflight(self, manifest: dict, approval: dict, *, initial: bool = False) -> dict:
        require(platform_id() == manifest["platform"], "runtime_platform_mismatch")
        for p in [*map(Path, self.stores.values()), self.root, *self.configs]:
            safe_path(p)
        require(Path(self.p["caddyfile"]).is_file(), "existing_caddy_configuration_required")
        require(
            all(Path(p).is_file() for p in self.p["unit_files"]) and self.p["unit_files"],
            "existing_service_units_required",
        )
        require(
            set(map(str, installed_unit_files(self.services))) == set(self.p["unit_files"]),
            "systemd_unit_inventory_drift",
        )
        old = self.current()
        if old is None:
            require(initial, "node_not_installed_use_initial_after_provisioning")
            require(
                not any(Path(p).exists() for p in self.stores.values()),
                "initial_install_requires_pristine_stores",
            )
        else:
            require(not initial, "initial_install_refuses_existing_release")
            meta = read_json(old / "RELEASE_META.json")
            old_sha = str(meta.get("commit_sha", ""))
            allowed = approval["policy"].get("upgrade_from", [])
            require(
                any(
                    SHA.fullmatch(s) and s.startswith(old_sha) and len(old_sha) >= 7
                    for s in allowed
                ),
                "current_release_not_in_upgrade_acceptance",
            )
            current_version = tuple(map(int, str(meta["version"]).split(".")))
            new_version = tuple(map(int, str(manifest["version"]).split(".")))
            require(new_version >= current_version, "binary_downgrade_blocked")
            self.client.health(self.p["health_url"], str(meta["release_id"]))
            for suffix in (".access-key", ".identity-anchor"):
                require(
                    Path(self.stores["auth"] + suffix).is_file(), "protected_identity_asset_missing"
                )
        versions = self.schemas()
        for name, version in versions.items():
            require(0 < version <= manifest["schemas"][name], "incompatible_storage_schema:" + name)
        if versions.get("auth", 3) < 3:
            raise ReleaseError("legacy_identity_migration_requires_separate_review")
        self.drained()
        self.admission_guard(manifest["source_sha"])
        reserve = self.p.get("reserve_bytes", 256 * 1024**2)
        require(type(reserve) is int and reserve >= 64 * 1024**2, "disk_reserve_too_small")
        backup_bytes = sum(Path(p).stat().st_size for p in self.stores.values() if Path(p).exists())
        backup_bytes += sum(p.stat().st_size for p in self.configs)
        # Per-filesystem accounting includes stage, consistent snapshot, WAL/migration headroom.
        needs: dict[int, tuple[Path, int]] = {}
        for path, size in [
            (self.root, manifest["unpacked_bytes"] + backup_bytes),
            *[
                (Path(p).parent, Path(p).stat().st_size if Path(p).exists() else 0)
                for p in self.stores.values()
            ],
        ]:
            parent = path
            while not parent.exists():
                parent = parent.parent
            dev = parent.stat().st_dev
            prior = needs.get(dev, (parent, 0))
            needs[dev] = (parent, prior[1] + size)
        for parent, size in needs.values():
            require(shutil.disk_usage(parent).free >= size + reserve, "insufficient_disk_space")
        peer_versions = self.check_peers(manifest, approval)
        if old is not None:
            self.ingress()
        return {
            "schemas": versions,
            "peers": peer_versions,
            "initial": initial,
            "backup_bytes": backup_bytes,
            "current": str(old) if old else None,
        }

    def check_peers(self, manifest: dict, approval: dict) -> dict:
        peers = self.p.get("peers", {})
        policy = approval["policy"].get("mesh", {})
        if peers:
            require(
                policy.get("mode") == "rolling" and policy.get("evidence_sha256"),
                "rolling_compatibility_unproved_use_coordinated_order",
            )
        compatible = policy.get("compatible_from", [])
        versions = {}
        for identity, url in peers.items():
            health = self.client.health(url)
            release = health["release"]
            suffix = release.rsplit("-", 1)[-1]
            allowed = [manifest["source_sha"], *compatible]
            require(
                any(
                    SHA.fullmatch(s) and len(suffix) >= 7 and s.startswith(suffix) for s in allowed
                ),
                "mesh_peer_version_incompatible:" + identity,
            )
            versions[identity] = release
        return versions

    def ingress(self) -> list[dict]:
        return self.client.ingress(self.p["public_base_url"])

    def stage(self, bundle: Path, destination: Path, manifest: dict) -> None:
        unpack(bundle, destination, manifest)
        env = clean_env(destination)
        # Import checks stay isolated from production env, dotenv, DB and runtime config.
        validation = destination / ".preflight"
        validation.mkdir(mode=0o700)
        env.update(
            {
                "TERMINAL_MCP_ENV_FILE_PATH": str(validation / "env"),
                "TERMINAL_MCP_RUNTIME_CONFIG_PATH": str(validation / "runtime.env"),
                "TERMINAL_MCP_DATABASE_PATH": str(validation / "runtime.sqlite3"),
                "TERMINAL_MCP_AUTH_DATABASE_PATH": str(validation / "auth.sqlite3"),
                "TERMINAL_MCP_OUTPUT_CACHE_PATH": str(validation / "output.sqlite3"),
                "TERMINAL_MCP_FLEET_CONTROL_PATH": str(validation / "control.sqlite3"),
                "TERMINAL_MCP_FLEET_NODE_META_PATH": str(validation / "meta.sqlite3"),
                "TERMINAL_MCP_FLEET_PROJECTION_PATH": str(validation / "projection.sqlite3"),
                "TERMINAL_MCP_LOG_PATH": str(validation / "app.log"),
                "TERMINAL_MCP_CWD": str(validation),
            }
        )
        run(
            runtime_command(
                destination,
                "-c",
                "import terminal_mcp.app; import sqlite3; "
                "assert sqlite3.sqlite_version == '3.53.4'",
            ),
            env=env,
            cwd=validation,
            timeout=60,
            code="isolated_runtime_smoke",
        )
        shutil.rmtree(validation)
        atomic(destination / "DEPLOYED_ARTIFACT.json", encoded(manifest))

    def backup(self, release: Path, destination: Path) -> None:
        # Use the accepted native SQLite build for snapshots, never the system SQLite.
        env = clean_env(destination.parent)
        run(
            runtime_command(
                release,
                "-m",
                "terminal_mcp.deployment.release",
                "_snapshot",
                "--profile",
                str(self.profile_path),
                "--destination",
                str(destination),
            ),
            env=env,
            cwd=destination.parent,
            timeout=120,
            code="consistent_snapshot",
        )

    def migrate(self, release: Path, backup: Path, receipt: Path, source_sha: str) -> None:
        env = clean_env(receipt.parent)
        env.update(self.values)
        env["TERMINAL_MCP_ENV_FILE_PATH"] = self.p["env_file"]
        run(
            runtime_command(
                release,
                "-m",
                "terminal_mcp.deployment.release",
                "_migrate",
                "--profile",
                str(self.profile_path),
                "--backup",
                str(backup),
                "--receipt",
                str(receipt),
                "--sha",
                source_sha,
            ),
            env=env,
            cwd=receipt.parent,
            timeout=120,
            code="migration",
        )

    def verify_migration(self, backup: Path, receipt: Path, manifest: dict) -> None:
        r = read_json(receipt)
        require(
            r.get("source_sha") == manifest["source_sha"] and r.get("completed") is True,
            "migration_receipt_missing_or_mismatched",
        )
        require(
            r.get("snapshot_sha256") == sha256(backup / "snapshot.json"),
            "migration_snapshot_mismatch",
        )
        versions = self.schemas()
        require(
            all(versions.get(k) == v for k, v in r["schemas"].items()), "migration_schema_drift"
        )
        require(
            all(v == manifest["schemas"][k] for k, v in versions.items()),
            "migration_target_schema_mismatch",
        )
        snap = read_json(backup / "snapshot.json")
        for name, data in snap["stores"].items():
            old = backup / data["backup"]
            require(sha256(old) == data["sha256"], "backup_tampered")
            if name != "output":
                preserved_rows(old, Path(data["path"]))
        for path, data in snap["configs"].items():
            require(sha256(Path(path)) == data["sha256"], "protected_configuration_changed")

    def switch(self, release: Path) -> None:
        temporary = self.root / ".current-release-new"
        if temporary.is_symlink():
            require(Path(os.readlink(temporary)) == release, "switch_intent_conflict")
        else:
            require(not temporary.exists(), "switch_intent_conflict")
            temporary.symlink_to(release)
        os.replace(temporary, self.root / "current")
        sync_dir(self.root)

    def ready(self, manifest: dict) -> dict:
        timeout = self.p.get("ready_timeout", 60)
        require(isinstance(timeout, (int, float)) and 1 <= timeout <= 300, "invalid_ready_timeout")
        deadline = time.monotonic() + timeout
        error = "readiness_timeout"
        while True:
            try:
                if all(self.active(unit) for unit in self.services):
                    return self.client.health(self.p["health_url"], manifest["release"])
            except ReleaseError as exc:
                error = str(exc)
            if time.monotonic() >= deadline:
                raise ReleaseError(error)
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))


class Update:
    def __init__(
        self,
        driver: NodeDriver,
        bundle: Path,
        approval: Path,
        approval_pin: str,
        source_sha: str,
        attempt: int = 1,
    ):
        require(type(attempt) is int and 1 <= attempt <= 10000, "invalid_attempt_number")
        self.driver, self.bundle = driver, bundle
        self.manifest, self.approval = verify_approval(bundle, approval, approval_pin, source_sha)
        self.identity = source_sha + "-" + self.manifest["payload_sha256"][:12]
        if attempt > 1:
            self.identity += "-attempt-" + str(attempt)
        self.area = safe_path(driver.root / "deployments" / self.identity)
        self.journal = self.area / "operation.json"
        self.release = driver.root / "releases" / self.identity
        self.backup = self.area / "backup"
        self.receipt = self.area / "migration.json"
        self.binding = {
            "identity": self.identity,
            "target": driver.p["target"],
            "profile_sha256": sha256(driver.profile_path),
            "approval_sha256": approval_pin,
        }

    def load(self) -> dict | None:
        if not self.journal.exists():
            return None
        safe_path(self.journal, owner=True)
        record = read_json(self.journal)
        require(record.get("binding") == self.binding, "operation_binding_changed")
        require(record.get("phase") in PHASES, "unknown_operation_phase")
        return record

    @contextmanager
    def lock(self):
        parent = safe_path(self.driver.root / "deployments")
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = safe_path(parent / ".node.lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ReleaseError("deployment_already_running") from exc
            # Block another artifact's unfinished update, not just this artifact's journal.
            for p in parent.glob("*/operation.json"):
                if p != self.journal:
                    old = read_json(p)
                    require(
                        old.get("phase") in {"applied", "aborted"},
                        "another_deployment_requires_reconciliation",
                    )
            yield
        finally:
            os.close(fd)

    def save(self, record: dict, phase: str) -> None:
        record.update({"phase": phase, "updated_at": utc()})
        record.pop("error", None)
        atomic(self.journal, encoded(record))

    def status(self) -> dict:
        record = self.load()
        current = self.driver.current()
        outcome = "not_applied"
        observations = {
            "current_is_candidate": current == self.release,
            "migration_receipt": self.receipt.is_file(),
        }
        if record:
            outcome = (
                "partially_applied"
                if record["phase"] not in {"prepared", "staging", "staged", "aborted"}
                else "not_applied"
            )
            if current == self.release:
                outcome = "partially_applied"
                try:
                    health = self.driver.client.health(
                        self.driver.p["health_url"], self.manifest["release"]
                    )
                    observations["health"] = health
                    if record["phase"] == "applied" and all(
                        self.driver.active(u) for u in self.driver.services
                    ):
                        self.driver.ingress()
                        outcome = "applied"
                except ReleaseError as exc:
                    observations["probe_error"] = str(exc)
        return {
            "outcome": outcome,
            "phase": record["phase"] if record else "absent",
            "operation": self.identity,
            "target": self.driver.p["target"],
            "observations": observations,
            "error": record.get("error") if record else None,
        }

    def plan(self, *, initial: bool = False) -> dict:
        record = self.load()
        if record:
            return self.status() | {"next_action": "status_then_explicit_resume_or_recover"}
        result = self.driver.preflight(self.manifest, self.approval, initial=initial)
        return {
            "outcome": "not_applied",
            "ready": True,
            "source_sha": self.manifest["source_sha"],
            "operation": self.identity,
            "preflight": result,
            "mutations": 0,
        }

    def window(self, confirmation: str) -> None:
        # No scheduler or caller can silently invent authorization for a restart.
        require(
            confirmation == self.driver.p["target"] + ":idle-window",
            "coordinated_idle_window_confirmation_required",
        )

    def apply(self, confirmation: str, *, initial: bool = False) -> dict:
        self.window(confirmation)
        with self.lock():
            old = self.load()
            if old:
                if old["phase"] == "applied":
                    return self.status()
                raise ReleaseError("unfinished_operation_use_status_not_blind_retry")
            preflight = self.driver.preflight(self.manifest, self.approval, initial=initial)
            record = {
                "format": 1,
                "binding": self.binding,
                "created_at": utc(),
                "before": preflight,
                "initial": initial,
            }
            self.save(record, "prepared")
            try:
                self.save(record, "staging")
                self.driver.stage(self.bundle, self.release, self.manifest)
                self.save(record, "staged")
                # Recheck after potentially expensive extraction, before any service mutation.
                self.driver.drained()
                self.driver.admission_guard(self.manifest["source_sha"])
                self.driver.check_peers(self.manifest, self.approval)
                self.driver.admission_guard(self.manifest["source_sha"])
                self.save(record, "stopping")
                self.driver.control("stop", self.driver.services[-1])
                require(not self.driver.active(self.driver.services[-1]), "api_stop_unconfirmed")
                self.driver.drained()
                for unit in reversed(self.driver.services[:-1]):
                    self.driver.control("stop", unit)
                    require(not self.driver.active(unit), "executor_stop_unconfirmed")
                self.save(record, "stopped")
                self.save(record, "backing_up")
                self.driver.backup(self.release, self.backup)
                self.save(record, "backed_up")
                self.save(record, "migrating")
                self.driver.migrate(
                    self.release, self.backup, self.receipt, self.manifest["source_sha"]
                )
                self.driver.verify_migration(self.backup, self.receipt, self.manifest)
                self.save(record, "migrated")
                return self.forward(record)
            except Exception as exc:
                record["error"] = (
                    str(exc) if isinstance(exc, ReleaseError) else "unexpected_operation_failure"
                )
                atomic(self.journal, encoded(record))
                raise ReleaseError(record["error"]) from exc

    def forward(self, record: dict) -> dict:
        if self.driver.current() != self.release:
            require(
                all(not self.driver.active(u) for u in self.driver.services),
                "services_must_be_stopped_before_switch",
            )
            self.driver.verify_migration(self.backup, self.receipt, self.manifest)
            self.save(record, "switching")
            self.driver.switch(self.release)
            self.save(record, "switched")
        self.save(record, "starting")
        for unit in self.driver.services:
            # Reconcile a timed-out start with observed state; never issue a second restart.
            if not self.driver.active(unit):
                self.driver.control("start", unit)
        self.save(record, "verifying")
        self.driver.ready(self.manifest)
        self.driver.ingress()
        self.driver.check_peers(self.manifest, self.approval)
        self.save(record, "applied")
        return self.status()

    def resume(self, confirmation: str) -> dict:
        self.window(confirmation)
        with self.lock():
            record = self.load()
            require(record is not None, "operation_not_found")
            if record["phase"] == "applied":
                return self.status()
            if record["phase"] == "migrating":
                require(
                    self.receipt.is_file(),
                    "migration_outcome_unknown_offline_reconciliation_required",
                )
                require(
                    all(not self.driver.active(u) for u in self.driver.services),
                    "services_must_be_stopped_for_migration_reconciliation",
                )
                self.driver.verify_migration(self.backup, self.receipt, self.manifest)
                self.save(record, "migrated")
            require(record["phase"] in FORWARD_PHASES, "pre_migration_failure_use_recover")
            return self.forward(record)

    def recover(self, confirmation: str) -> dict:
        self.window(confirmation)
        with self.lock():
            record = self.load()
            require(record is not None, "operation_not_found")
            if record["phase"] == "aborted":
                return self.status()
            require(
                record["phase"] in BEFORE_MIGRATION,
                "database_rollback_forbidden_use_forward_recovery",
            )
            expected = record["before"]["current"]
            require(
                str(self.driver.current()) == expected
                if expected
                else self.driver.current() is None,
                "current_release_changed",
            )
            if expected:
                self.driver.drained()
                for unit in self.driver.services:
                    if not self.driver.active(unit):
                        self.driver.control("start", unit)
            # No restore, delete or replay. The failed release and backups remain available.
            self.save(record, "aborted")
            return self.status()
