"""Authenticated peer transport configuration for Mesh control and data."""

import logging
import secrets

from terminal_mcp.fleet.config import FleetConfig, FleetPeer

_PEER_AUTH_LOG = logging.getLogger(__name__)


class MeshPeerTransport:
    def __init__(self, config: FleetConfig):
        self.config = config
        self._auth_denials_reported: set[tuple[str, str]] = set()

    def authenticate(self, peer_instance_id: str, authorization: str) -> FleetPeer | None:
        peer = self.config.peers_by_id.get(peer_instance_id)
        prefix = "bearer "
        has_bearer = authorization.lower().startswith(prefix)
        token = authorization[len(prefix) :].strip() if has_bearer else ""
        matches = bool(peer and token and secrets.compare_digest(token, peer.auth_token))
        if not matches:
            reason = (
                "unknown_peer" if peer is None else
                "missing_bearer" if not has_bearer else "missing_token" if not token else "mismatch"
            )
            safe_peer = (
                peer_instance_id if peer_instance_id in self.config.peers_by_id else "<other>"
            )
            key = (safe_peer, reason)
            if key not in self._auth_denials_reported:
                self._auth_denials_reported.add(key)
                _PEER_AUTH_LOG.warning(
                    "fleet_peer_auth_rejected local_node=%s peer=%s reason=%s "
                    "header_peer_chars=%s has_bearer=%s supplied_token_chars=%s "
                    "expected_token_chars=%s",
                    self.config.instance_id,
                    safe_peer,
                    reason,
                    len(peer_instance_id),
                    has_bearer,
                    len(token),
                    len(peer.auth_token) if peer else 0,
                )
            return None
        return peer
