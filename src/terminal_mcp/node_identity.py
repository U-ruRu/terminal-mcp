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
import secrets
import tempfile
from pathlib import Path


def local_namespace(node_id: str) -> str:
    value = "local-" + node_id
    return (
        value if len(value) <= 64 else "local-" + hashlib.sha256(node_id.encode()).hexdigest()[:32]
    )


def _identity(path: Path, settings) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {
            "node_id": settings.fleet_instance_id.strip() or "node-" + secrets.token_hex(12),
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
    # Source/control are always present, including an autonomous node. These
    # values describe available protocols, not a second agent runtime.
    settings.fleet_v1_source_enabled = True
    settings.fleet_v1_authority_enabled = True
