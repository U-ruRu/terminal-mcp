"""Resolve an MCP Events subscriber through existing Access Mesh attachments.

The binding is the same role/node/principal/provider-key tuple used by tools.
No new recipient grant, session naming rule, or identity database is created.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import aiosqlite
from mcp.types import RequestParams

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.core.persistent_admission import VerifiedAdmissionContext
from terminal_mcp.core.provider_identity import ProviderIdentityError, ProviderIdentityRegistry


@dataclass(frozen=True, slots=True)
class MessageSubscriber:
    recipient_agent_id: str
    work_session_id: str
    session_epoch: int
    connection_key: str
    issuer_id: str
    slot_id: str
    role: str


def provider_metadata_from_events(params: object) -> dict[str, str] | None:
    """Validate standard MCP request _meta; never accept identity as arguments.

    This metadata is an input to the existing Access Mesh binding lookup, not
    standalone authorization. The authenticated OAuth client must match the
    principal recorded during attach.
    """
    if not isinstance(params, dict):
        return None
    raw = params.get("_meta")
    if not isinstance(raw, dict):
        return None
    try:
        meta = RequestParams.Meta.model_validate(raw)
        dumped = meta.model_dump(exclude_none=True)
        evidence = {
            name: dumped[name]
            for name in ("openai/subject", "openai/session", "openai/organization")
            if name in dumped
        }
        ProviderIdentityRegistry().resolve("openai", evidence)
    except (TypeError, ValueError, ProviderIdentityError):
        return None
    return evidence


class MeshMessageSubscriberResolver:
    """Read-only resolution of established node-local Access/attach bindings."""

    def __init__(self, access_mesh):
        self.mesh = access_mesh

    async def _lookup(self, key: str) -> MessageSubscriber | None:
        # One read-only statement ensures slot, attachment and WorkSession are
        # mutually consistent. Reading must not materialize/touch an agent.
        path = self.mesh.store.path
        node = self.mesh.store.local_node_id
        async with aiosqlite.connect(path, timeout=3) as db:
            row = await (
                await db.execute(
                    "SELECT a.logical_agent_id,l.work_session_id,l.session_epoch,"
                    "a.issuer_id,a.slot_id "
                    "FROM access_mesh_attachments a "
                    "JOIN access_mesh_slot_replicas r ON "
                    "r.issuer_id=a.issuer_id AND r.slot_id=a.slot_id "
                    "JOIN access_mesh_local_sessions l ON "
                    "l.issuer_id=a.issuer_id AND l.slot_id=a.slot_id "
                    "JOIN logical_agent_work_sessions s ON "
                    "s.work_session_id=l.work_session_id AND "
                    "s.session_epoch=l.session_epoch AND "
                    "s.logical_agent_id=a.logical_agent_id "
                    "WHERE a.connection_key=? AND r.state='active' "
                    "AND s.state='active' AND s.authority_node_id=? "
                    "AND s.hard_expires_at>? LIMIT 1",
                    (key, node, utc_text()),
                )
            ).fetchone()
        if row is None:
            return None
        return MessageSubscriber(
            recipient_agent_id=row[0],
            work_session_id=row[1],
            session_epoch=int(row[2]),
            connection_key=key,
            issuer_id=row[3],
            slot_id=row[4],
            role="coordinator",
        )

    async def resolve(
        self,
        admission: VerifiedAdmissionContext | None,
        provider_metadata: dict | None,
        endpoint_role: str,
    ) -> MessageSubscriber | None:
        if (
            admission is None
            or admission.transport != "mcp"
            or "terminal:read" not in admission.scopes
            or not provider_metadata
            or endpoint_role not in {"access", "executor", "coordinator"}
        ):
            return None
        actor = ActorContext.from_admission(
            admission,
            node_id=self.mesh.store.local_node_id,
            endpoint_role=endpoint_role,
            provider="openai",
            provider_metadata=provider_metadata,
        )
        try:
            # Same exact key derivation used by AccessMesh.attach on the role.
            key = self.mesh.connection_key(actor)
        except (ValueError, TypeError):
            return None
        if endpoint_role == "access":
            # Access's own subject+conversation was bound when the session
            # number was issued. Its connection is an issuer binding, not
            # the separate execution/coordination attach binding.
            slot = await asyncio.to_thread(self.mesh.store.issuer_bound_slot, key)
            if slot is None or slot.state != "active":
                return None
            return MessageSubscriber(
                recipient_agent_id=slot.logical_agent_id,
                work_session_id="",
                session_epoch=0,
                connection_key=key,
                issuer_id=slot.issuer_id,
                slot_id=slot.slot_id,
                role="access",
            )
        local = await self._lookup(key)
        if local is None:
            return None
        return MessageSubscriber(
            recipient_agent_id=local.recipient_agent_id,
            work_session_id=local.work_session_id,
            session_epoch=local.session_epoch,
            connection_key=local.connection_key,
            issuer_id=local.issuer_id,
            slot_id=local.slot_id,
            role=endpoint_role,
        )

    def _number_slot(self, number: str, *, require_live: bool):
        # The number is a public routing selector inside the existing authorized
        # server space. Never attach, materialize, or infer a caller's agent.
        winner = self.mesh.store.numbers.winner(number)
        if winner is None:
            return None
        slot = self.mesh.store.code_slot(winner["issuer_id"], number)
        if (
            slot is None
            or slot.state != "active"
            or slot.slot_id != winner["slot_id"]
            or slot.logical_agent_id != winner["logical_agent_id"]
        ):
            return None
        if require_live:
            cycle = self.mesh.store.merged_cycle(slot, self.mesh.clock())
            if cycle["state"] not in {"active", "warning", "draining"}:
                return None
        return slot

    async def resolve_number(
        self, admission: VerifiedAdmissionContext | None, number: str, endpoint_role: str
    ) -> MessageSubscriber | None:
        if (
            admission is None
            or admission.transport != "mcp"
            or "terminal:read" not in admission.scopes
            or endpoint_role != "access"
        ):
            return None
        try:
            slot = await asyncio.to_thread(self._number_slot, number, require_live=True)
        except AccessMeshError:
            return None
        if slot is None:
            return None
        return MessageSubscriber(
            recipient_agent_id=slot.logical_agent_id,
            work_session_id="",
            session_epoch=0,
            connection_key="session-number:" + number,
            issuer_id=slot.issuer_id,
            slot_id=slot.slot_id,
            role="session_number",
        )

    async def number_reassigned(self, delivery: dict) -> bool:
        """Keep an old recipient association visible to an explicit refresh."""
        key = delivery.get("recipient_connection_key") or ""
        if (
            delivery.get("recipient_role") != "session_number"
            or delivery.get("recipient_node_id") != self.mesh.store.local_node_id
            or not key.startswith("session-number:")
        ):
            return False
        winner = await asyncio.to_thread(
            self.mesh.store.numbers.winner, key.removeprefix("session-number:")
        )
        return bool(
            winner
            and (
                winner["logical_agent_id"],
                winner["issuer_id"],
                winner["slot_id"],
            ) != (
                delivery.get("recipient_agent_id"),
                delivery.get("recipient_issuer_id"),
                delivery.get("recipient_slot_id"),
            )
        )

    async def still_bound(self, delivery: dict) -> bool:
        key = delivery.get("recipient_connection_key")
        if not key or delivery.get("recipient_node_id") != self.mesh.store.local_node_id:
            return False
        role = delivery.get("recipient_role")
        if role == "session_number":
            if not key.startswith("session-number:"):
                return False
            try:
                slot = await asyncio.to_thread(
                    self._number_slot,
                    key.removeprefix("session-number:"),
                    require_live=bool(delivery.get("recipient_require_live")),
                )
            except AccessMeshError:
                return False
            # Ordinary end/start changes WorkSession epochs, not this recipient.
            # Number release/reuse never redirects an existing callback.
            return bool(
                slot
                and slot.logical_agent_id == delivery.get("recipient_agent_id")
                and slot.issuer_id == delivery.get("recipient_issuer_id")
                and slot.slot_id == delivery.get("recipient_slot_id")
            )
        if role == "access":
            slot = await asyncio.to_thread(self.mesh.store.issuer_bound_slot, key)
            return bool(
                slot
                and slot.state == "active"
                and slot.logical_agent_id == delivery.get("recipient_agent_id")
                and slot.issuer_id == delivery.get("recipient_issuer_id")
                and slot.slot_id == delivery.get("recipient_slot_id")
            )
        current = await self._lookup(key)
        return bool(
            role in {"executor", "coordinator"}
            and current
            and current.recipient_agent_id == delivery.get("recipient_agent_id")
            and current.work_session_id == delivery.get("recipient_session_id")
            and current.session_epoch == delivery.get("recipient_epoch")
            and current.issuer_id == delivery.get("recipient_issuer_id")
            and current.slot_id == delivery.get("recipient_slot_id")
        )
