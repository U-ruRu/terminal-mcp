"""Authoritative Terminal MCP backup inventory and source validation.

This module intentionally does not copy or restore files. It defines the durable
state that a consistent backup operation must protect and provides fail-closed,
read-only validation that can be reused by installer/restore tooling.
"""

from __future__ import annotations

import sqlite3
import stat
from dataclasses import asdict, dataclass
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
