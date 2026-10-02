from __future__ import annotations

import base64
import ipaddress
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


@dataclass(frozen=True, slots=True)
class FleetPeer:
    instance_id: str
    origin: str
    public_key: str
    auth_token: str


@dataclass(frozen=True, slots=True)
class FleetConfig:
    instance_id: str
    signing_private_key: str
    peers: tuple[FleetPeer, ...]
    replication_interval_seconds: float
    request_timeout_seconds: float
    local_auth_token: str | None = None

    @property
    def peers_by_id(self) -> dict[str, FleetPeer]:
        return {peer.instance_id: peer for peer in self.peers}

    def outbound_auth_token(self, peer: FleetPeer) -> str:
        return self.local_auth_token or peer.auth_token


_INSTANCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _instance_id(value: str, label: str) -> str:
    normalized = value.strip()
    if not _INSTANCE_ID.fullmatch(normalized):
        raise ValueError(f"{label} must be 1-64 ASCII letters, digits, dot, underscore or dash")
    return normalized


def _key_bytes(value: str, label: str) -> bytes:
    normalized = value.strip()
    try:
        raw = base64.urlsafe_b64decode(normalized + "=" * (-len(normalized) % 4))
    except Exception as exc:
        raise ValueError(f"{label} must be URL-safe base64 Ed25519 key material") from exc
    if len(raw) != 32:
        raise ValueError(f"{label} must decode to exactly 32 bytes")
    return raw


def _validate_private_key(value: str) -> str:
    normalized = value.strip()
    Ed25519PrivateKey.from_private_bytes(_key_bytes(normalized, "fleet_signing_private_key"))
    return normalized


def _validate_public_key(value: str, index: int) -> str:
    normalized = value.strip()
    Ed25519PublicKey.from_public_bytes(_key_bytes(normalized, f"fleet peer {index} public_key"))
    return normalized


def _loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("fleet peer origin must be an explicit http/https origin")
    if (
        parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("fleet peer origin must not contain path, credentials, query or fragment")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("fleet peer origin has an invalid port") from exc
    if parsed.scheme == "http" and not _loopback_host(parsed.hostname):
        raise ValueError("fleet peer origin must use HTTPS except for loopback development")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def build_fleet_config(settings) -> FleetConfig | None:
    raw_instance_id = settings.fleet_instance_id.strip()
    raw_private_key = settings.fleet_signing_private_key.strip()
    raw_peers = settings.fleet_peers_json.strip() or "[]"
    try:
        payload = json.loads(raw_peers)
    except json.JSONDecodeError as exc:
        raise ValueError("fleet_peers_json must be valid JSON") from exc
    if not isinstance(payload, list):
        raise ValueError("fleet_peers_json must be a JSON array")

    if not raw_instance_id and not raw_private_key and not payload:
        return None
    if not raw_instance_id:
        raise ValueError("fleet_instance_id is required when fleet federation is configured")
    if not raw_private_key:
        raise ValueError(
            "fleet_signing_private_key is required when fleet federation is configured"
        )
    instance_id = _instance_id(raw_instance_id, "fleet_instance_id")
    private_key = _validate_private_key(raw_private_key)

    peers: list[FleetPeer] = []
    seen = {instance_id}
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"fleet peer {index} must be an object")
        raw_peer_id = str(item.get("instance_id", "")).strip()
        origin = str(item.get("origin", "")).strip()
        raw_public_key = str(item.get("public_key", "")).strip()
        auth_token = str(item.get("auth_token", "")).strip()
        if not all((raw_peer_id, origin, raw_public_key, auth_token)):
            raise ValueError(
                f"fleet peer {index} requires instance_id, origin, public_key and auth_token"
            )
        peer_id = _instance_id(raw_peer_id, f"fleet peer {index} instance_id")
        public_key = _validate_public_key(raw_public_key, index)
        if any(ch.isspace() for ch in auth_token):
            raise ValueError(f"fleet peer {index} auth_token must not contain whitespace")
        if peer_id in seen:
            raise ValueError(f"duplicate fleet instance_id: {peer_id}")
        seen.add(peer_id)
        peers.append(FleetPeer(peer_id, _origin(origin), public_key, auth_token))

    interval = float(settings.fleet_replication_interval_sec)
    timeout = float(settings.fleet_request_timeout_sec)
    if interval <= 0:
        raise ValueError("fleet_replication_interval_sec must be positive")
    if timeout <= 0:
        raise ValueError("fleet_request_timeout_sec must be positive")
    return FleetConfig(instance_id, private_key, tuple(peers), interval, timeout)
