"""Authoritative Terminal MCP backup inventory and source validation.

This module intentionally does not copy or restore files. It defines the durable
state that a consistent backup operation must protect and provides fail-closed,
read-only validation that can be reused by installer/restore tooling.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from terminal_mcp.auth.access_key import AccessVerifierKeyStore
from terminal_mcp.auth.foundation import AuthFoundationError
from terminal_mcp.config import Settings

BACKUP_MANIFEST_VERSION = 1
BackupKind = Literal["sqlite", "secret", "config"]


class BackupValidationError(RuntimeError):
    """Raised when authoritative state cannot be backed up safely."""


@dataclass(frozen=True, slots=True)
class BackupMember:
    member_id: str
    path: Path
    role: str
    kind: BackupKind
    required: bool
    secret: bool
    restore_order: int

    def public_dict(self) -> dict:
        payload = asdict(self)
        payload["path"] = str(self.path)
        return payload


def access_verifier_key_path(auth_database_path: Path) -> Path:
    path = Path(auth_database_path)
    return path.with_name(path.name + ".access-key")


def _fleet_state_required(settings: Settings) -> bool:
    return any(
        (
            settings.fleet_v1_source_enabled,
            settings.fleet_v1_authority_enabled,
            settings.fleet_v1_projection_enabled,
            settings.fleet_v1_public_enabled,
            settings.effective_fleet_control_path().exists(),
            settings.effective_fleet_node_meta_path().exists(),
        )
    )


def authoritative_backup_members(settings: Settings) -> tuple[BackupMember, ...]:
    """Return the canonical authoritative backup set in restore order."""

    fleet_required = _fleet_state_required(settings)
    auth_key = access_verifier_key_path(settings.auth_database_path)
    return (
        BackupMember(
            "node_environment",
            settings.env_file_path,
            (
                "Node identity, transport/auth secrets, Fleet bootstrap identity "
                "and deployment config."
            ),
            "config",
            True,
            True,
            10,
        ),
        BackupMember(
            "runtime_database",
            settings.database_path,
            "Authoritative commands, tasks, context, events and runtime coordination state.",
            "sqlite",
            True,
            True,
            20,
        ),
        BackupMember(
            "auth_database",
            settings.auth_database_path,
            "Rollback-excluded principals, grants, Access slots/verifiers and auth security state.",
            "sqlite",
            True,
            True,
            30,
        ),
        BackupMember(
            "access_verifier_key",
            auth_key,
            "Root-local key required to verify existing Access codes in auth state.",
            "secret",
            True,
            True,
            40,
        ),
        BackupMember(
            "fleet_node_meta",
            settings.effective_fleet_node_meta_path(),
            "Durable Fleet/node identity metadata and source sequencing state.",
            "sqlite",
            fleet_required,
            True,
            50,
        ),
        BackupMember(
            "fleet_control",
            settings.effective_fleet_control_path(),
            "Authoritative Mesh topology, trust, policy and managed Fleet private keys.",
            "sqlite",
            fleet_required,
            True,
            60,
        ),
        BackupMember(
            "runtime_config",
            settings.runtime_config_path,
            "Optional live diagnostics/logging overrides; preserved when present.",
            "config",
            False,
            False,
            70,
        ),
    )


def excluded_backup_members(settings: Settings) -> tuple[dict, ...]:
    """Durable-looking files that are explicitly reconstructible/disposable."""

    return (
        {
            "member_id": "output_cache",
            "path": str(settings.output_cache_path),
            "reason": (
                "Disposable command-output cache; authoritative command state lives in runtime DB."
            ),
        },
        {
            "member_id": "fleet_projection",
            "path": str(settings.effective_fleet_projection_path()),
            "reason": "Derived Fleet projection; rebuilt from authoritative source/control state.",
        },
        {
            "member_id": "log",
            "path": str(settings.log_path),
            "reason": "Operational diagnostics, not authoritative state.",
        },
    )


def authoritative_backup_manifest(settings: Settings) -> dict:
    """Machine-readable backup contract without mutating any source state."""

    return {
        "manifest_version": BACKUP_MANIFEST_VERSION,
        "members": [member.public_dict() for member in authoritative_backup_members(settings)],
        "excluded": list(excluded_backup_members(settings)),
    }


def _sqlite_quick_check(path: Path) -> dict:
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=1.0) as db:
            rows = [str(row[0]) for row in db.execute("PRAGMA quick_check")]
            version_row = db.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error as exc:
        raise BackupValidationError(f"{path}: sqlite validation failed: {exc}") from exc
    if rows != ["ok"]:
        raise BackupValidationError(f"{path}: sqlite quick_check failed: {rows}")
    return {"user_version": int(version_row[0]) if version_row else 0}


def _validate_secure_mode(member: BackupMember, mode: int) -> None:
    if not member.secret:
        return
    if mode & 0o077:
        raise BackupValidationError(
            f"{member.member_id}: insecure permissions {mode:o}; "
            "authoritative secret must be owner-only"
        )


def validate_authoritative_backup_source(settings: Settings) -> dict:
    """Validate live source state read-only before a consistent snapshot."""

    validated = []
    missing = []
    seen_paths: dict[Path, str] = {}

    for member in authoritative_backup_members(settings):
        path = member.path.resolve()
        previous = seen_paths.get(path)
        if previous is not None:
            raise BackupValidationError(
                f"backup members {previous} and {member.member_id} resolve to the same path: {path}"
            )
        seen_paths[path] = member.member_id

        if not path.exists():
            if member.required:
                missing.append(member.member_id)
            continue
        if not path.is_file():
            raise BackupValidationError(f"{member.member_id}: not a regular file: {path}")

        file_stat = path.stat()
        mode = stat.S_IMODE(file_stat.st_mode)
        _validate_secure_mode(member, mode)
        details = {
            "member_id": member.member_id,
            "path": str(path),
            "size": int(file_stat.st_size),
            "mode": f"{mode:04o}",
            "kind": member.kind,
            "restore_order": member.restore_order,
        }
        if member.kind == "sqlite":
            details.update(_sqlite_quick_check(path))
        elif member.member_id == "access_verifier_key":
            try:
                AccessVerifierKeyStore(path).load_or_create(allow_create=False)
            except AuthFoundationError as exc:
                raise BackupValidationError(f"access_verifier_key: {exc}") from exc
        validated.append(details)

    if missing:
        raise BackupValidationError(
            "missing required authoritative backup members: " + ", ".join(sorted(missing))
        )

    return {
        "manifest_version": BACKUP_MANIFEST_VERSION,
        "validated": validated,
        "excluded": list(excluded_backup_members(settings)),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_filename(member: BackupMember) -> str:
    suffix = member.path.suffix
    return f"{member.member_id}{suffix}" if suffix else member.member_id


def _copy_sqlite_snapshot(source: Path, destination: Path) -> None:
    source_uri = source.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True, timeout=5.0) as src:
            with sqlite3.connect(destination) as dst:
                src.backup(dst)
    except sqlite3.Error as exc:
        raise BackupValidationError(f"{source}: sqlite snapshot failed: {exc}") from exc


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_authoritative_snapshot(
    settings: Settings,
    destination: Path,
    *,
    quiesced: bool,
) -> dict:
    """Create one atomic snapshot directory from quiesced authoritative state.

    Cross-database consistency cannot be inferred from independent SQLite files.
    The caller must first quiesce Terminal MCP and explicitly assert that state.
    Refusing to run otherwise prevents a normal caller from silently producing a
    mixed-generation backup.
    """

    destination = Path(destination).resolve()
    if not quiesced:
        raise BackupValidationError("authoritative snapshot requires quiesced Terminal MCP state")
    if destination.exists():
        raise BackupValidationError(f"snapshot destination already exists: {destination}")

    source_validation = validate_authoritative_backup_source(settings)
    validated_by_id = {item["member_id"]: item for item in source_validation["validated"]}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.tmp-",
            dir=destination.parent,
        )
    )
    temp_root.chmod(0o700)
    files_root = temp_root / "files"
    files_root.mkdir(mode=0o700)

    snapshot_members = []
    try:
        for member in authoritative_backup_members(settings):
            source_info = validated_by_id.get(member.member_id)
            if source_info is None:
                continue
            source = member.path.resolve()
            filename = _snapshot_filename(member)
            target = files_root / filename

            if member.kind == "sqlite":
                _copy_sqlite_snapshot(source, target)
            else:
                shutil.copyfile(source, target)

            source_mode = int(source_info["mode"], 8)
            target.chmod(source_mode)
            _fsync_file(target)

            snapshot_details = {
                **member.public_dict(),
                "source_path": str(source),
                "snapshot_file": str(Path("files") / filename),
                "mode": f"{source_mode:04o}",
                "size": target.stat().st_size,
                "sha256": _sha256(target),
            }
            snapshot_details["path"] = str(source)
            if member.kind == "sqlite":
                snapshot_details.update(_sqlite_quick_check(target))
            snapshot_members.append(snapshot_details)

        manifest = {
            "manifest_version": BACKUP_MANIFEST_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "quiesced": True,
            "members": snapshot_members,
            "excluded": list(excluded_backup_members(settings)),
        }
        manifest_path = temp_root / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)
        _fsync_file(manifest_path)
        _fsync_directory(files_root)
        _fsync_directory(temp_root)
        temp_root.rename(destination)
        _fsync_directory(destination.parent)
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise

    return validate_authoritative_snapshot(destination)


def _safe_snapshot_file(root: Path, relative: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise BackupValidationError(
            f"snapshot member path must be relative and contained: {relative}"
        )
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise BackupValidationError(f"snapshot member escapes snapshot root: {relative}") from exc
    return path


def validate_authoritative_snapshot(snapshot_root: Path) -> dict:
    """Validate snapshot membership, checksums, modes and SQLite integrity."""

    root = Path(snapshot_root).resolve()
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupValidationError(f"invalid snapshot manifest: {manifest_path}: {exc}") from exc

    if manifest.get("manifest_version") != BACKUP_MANIFEST_VERSION:
        raise BackupValidationError(
            f"unsupported snapshot manifest version: {manifest.get('manifest_version')!r}"
        )
    if manifest.get("quiesced") is not True:
        raise BackupValidationError("snapshot manifest is not marked quiesced")

    members = manifest.get("members")
    if not isinstance(members, list):
        raise BackupValidationError("snapshot manifest members must be a list")

    by_id = {}
    for item in members:
        if not isinstance(item, dict):
            raise BackupValidationError("snapshot member must be an object")
        member_id = str(item.get("member_id") or "")
        if not member_id or member_id in by_id:
            raise BackupValidationError(f"invalid or duplicate snapshot member id: {member_id!r}")
        by_id[member_id] = item

    required_base = {
        "node_environment",
        "runtime_database",
        "auth_database",
        "access_verifier_key",
    }
    missing_base = sorted(required_base - set(by_id))
    if missing_base:
        raise BackupValidationError(
            "snapshot missing mandatory members: " + ", ".join(missing_base)
        )

    fleet_ids = {"fleet_node_meta", "fleet_control"}
    fleet_present = fleet_ids & set(by_id)
    if fleet_present and fleet_present != fleet_ids:
        missing = sorted(fleet_ids - fleet_present)
        raise BackupValidationError(
            "snapshot contains incomplete Fleet authority state; missing: " + ", ".join(missing)
        )

    validated = []
    for member_id, item in by_id.items():
        relative = str(item.get("snapshot_file") or "")
        path = _safe_snapshot_file(root, relative)
        if not path.is_file():
            raise BackupValidationError(f"{member_id}: snapshot file is missing: {relative}")
        actual_mode = stat.S_IMODE(path.stat().st_mode)
        expected_mode = int(str(item.get("mode") or ""), 8)
        if actual_mode != expected_mode:
            raise BackupValidationError(
                f"{member_id}: mode mismatch {actual_mode:04o}!={expected_mode:04o}"
            )
        actual_size = path.stat().st_size
        if actual_size != int(item.get("size", -1)):
            raise BackupValidationError(
                f"{member_id}: size mismatch {actual_size}!={item.get('size')}"
            )
        actual_sha = _sha256(path)
        if actual_sha != item.get("sha256"):
            raise BackupValidationError(f"{member_id}: checksum mismatch")

        kind = item.get("kind")
        details = {
            "member_id": member_id,
            "snapshot_file": relative,
            "size": actual_size,
            "mode": f"{actual_mode:04o}",
            "sha256": actual_sha,
        }
        if kind == "sqlite":
            details.update(_sqlite_quick_check(path))
        elif member_id == "access_verifier_key":
            try:
                AccessVerifierKeyStore(path).load_or_create(allow_create=False)
            except AuthFoundationError as exc:
                raise BackupValidationError(f"access_verifier_key: {exc}") from exc
        validated.append(details)

    return {
        "manifest_version": BACKUP_MANIFEST_VERSION,
        "created_at": manifest.get("created_at"),
        "quiesced": True,
        "validated": sorted(validated, key=lambda item: item["member_id"]),
        "excluded": manifest.get("excluded", []),
    }
