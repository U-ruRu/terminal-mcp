"""Durable independent node identity for the single runtime.

Generating transport credentials does not create Mesh membership. Topology is
owned by the paired application's control store; the local namespace is only the
registration boundary of an autonomous node.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import sqlite3
import tempfile
from pathlib import Path


def local_namespace(node_id: str) -> str:
    value = "local-" + node_id
    return (
        value if len(value) <= 64 else "local-" + hashlib.sha256(node_id.encode()).hexdigest()[:32]
    )


def _prior_node_id(settings) -> str:
    """Preserve the local authority name of a pre-ACS standalone installation."""
    if settings.fleet_instance_id.strip():
        return settings.fleet_instance_id.strip()
    if settings.fleet_node_id.strip():
        return settings.fleet_node_id.strip()
    path = settings.database_path
    hostname = socket.gethostname()
    if path.exists() and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", hostname):
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='logical_agents'"
            ).fetchone()
            if (
                exists
                and db.execute(
                    "SELECT 1 FROM logical_agents WHERE authority_node_id=? LIMIT 1", (hostname,)
                ).fetchone()
            ):
                return hostname
        finally:
            db.close()
    return "node-" + secrets.token_hex(12)


def _identity(path: Path, settings) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        database = settings.database_path
        if database.exists():
            with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as db:
                established = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='unified_runtime_migration'"
                ).fetchone()
                if established:
                    raise ValueError(
                        "durable node identity is missing from an established install"
                    ) from None
        data = {
            "node_id": _prior_node_id(settings),
            "signing_private_key": settings.fleet_signing_private_key.strip()
            or base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
            "access_proof_key": settings.access_mesh_proof_key or secrets.token_urlsafe(48),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".node-identity-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Publish complete content once. Concurrent constructors use the
                # same winning identity, never a partially written credential.
                os.link(temporary, path)
                if os.name != "nt":
                    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            except FileExistsError:
                return _identity(path, settings)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if (
        not isinstance(data, dict)
        or set(data) != {"node_id", "signing_private_key", "access_proof_key"}
        or any(not isinstance(value, str) or not value for value in data.values())
        or len(data["access_proof_key"].encode()) < 32
    ):
        raise ValueError("invalid durable node identity")
    return data


def ensure_node_identity(settings) -> None:
    """Fill absent bootstrap credentials while preserving established identity."""
    data = _identity(settings.database_path.with_name("node-identity.json"), settings)
    for attribute, key in (
        ("fleet_instance_id", "node_id"),
        ("fleet_signing_private_key", "signing_private_key"),
        ("access_mesh_proof_key", "access_proof_key"),
    ):
        configured = getattr(settings, attribute)
        if configured and configured != data[key]:
            raise ValueError("configured credentials conflict with durable node identity")
        setattr(settings, attribute, data[key])
    settings.fleet_node_id = settings.fleet_node_id.strip() or settings.fleet_instance_id
    settings.fleet_id = settings.fleet_id.strip() or local_namespace(settings.fleet_node_id)
    settings.fleet_control_node_id = (
        settings.fleet_control_node_id.strip() or settings.fleet_node_id
    )
