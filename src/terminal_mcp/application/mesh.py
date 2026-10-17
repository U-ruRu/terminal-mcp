"""Transport-neutral authentication and errors for Mesh peers."""

from __future__ import annotations

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.persistent_admission import (
    current_admission_context,
)
from terminal_mcp.storage.persistent_agents import PersistentStoreError


class MeshApplicationError(Exception):
    """Transport-neutral rejection; adapters choose their own wire status."""

    def __init__(self, kind: str, detail: str | dict):
        self.kind = kind
        self.detail = detail
        super().__init__(str(detail))


class MeshApplication:
    """Shared authenticated peer boundary checks for Mesh control and data."""

    @staticmethod
    def _effective_actor(actor: ActorContext) -> ActorContext:
        return ActorContext.from_admission(
            current_admission_context(),
            transport=actor.transport,
            node_id=actor.node_id,
            endpoint_role=actor.endpoint_role,
            contract_version=actor.contract_version,
            peer_node_id=actor.peer_node_id,
            request_id=actor.request_id,
            provider=actor.provider,
        )

    @staticmethod
    def _require_peer(actor: ActorContext) -> None:
        if actor.endpoint_role != "mesh" or not actor.peer_node_id:
            raise MeshApplicationError("unauthorized", "invalid fleet peer")

    @staticmethod
    def raise_store_error(exc: PersistentStoreError):
        kind = (
            "not_found"
            if exc.code
            in {
                "slot_not_found",
                "session_not_found",
                "authority_unavailable",
                "message_not_found",
            }
            else "forbidden"
            if exc.code == "persistent_auth_required"
            else "conflict"
        )
        detail = {"code": exc.code}
        if exc.blockers:
            detail["blockers"] = exc.blockers
        raise MeshApplicationError(kind, detail) from exc
