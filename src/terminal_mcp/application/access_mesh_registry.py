"""Common ACS entry points and topology ports for the current registry."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.access_mesh_grants import AccessMeshError

REGISTRY_BINDING = ContextVar("mesh_registry_binding", default=None)
_ACTOR_FIELDS = {
    "principal_id",
    "credential_id",
    "scopes",
    "auth_generation",
    "auth_mode",
    "provider",
    "provider_metadata",
    "transport",
    "endpoint_role",
    "contract_version",
}


class AccessRegistryApplication:
    @property
    def registry_authoritative(self):
        return self.store.registry_authority_id == self.store.local_node_id

    async def proxy_registry(self, actor, operation, arguments):
        """Return None locally; a member forwards only already admitted actors."""
        await asyncio.to_thread(self.store.assert_registry_ready)
        if self.registry_authoritative:
            if operation not in {"slots", "get", "defaults"}:
                await asyncio.to_thread(self.store.assert_registry_available)
            return None
        if self.replication is None:
            raise AccessMeshError("mesh_primary_unavailable")
        admission = actor.admission()
        if admission is None:
            raise AccessMeshError("persistent_auth_required")
        admission.require(
            "terminal:read" if operation in {"slots", "get", "defaults"} else "terminal:execute"
        )
        payload = {name: getattr(actor, name) for name in _ACTOR_FIELDS}
        payload["scopes"] = sorted(actor.scopes)
        payload["provider_metadata"] = dict(actor.provider_metadata)
        return await self.replication.registry_request(
            {
                "namespace_id": self.store.namespace_id,
                "operation": operation,
                "arguments": arguments,
                "actor": payload,
                "connection_key": self.connection_key(actor),
            }
        )

    async def accept_registry_request(self, payload, *, authenticated_peer_id):
        """A trusted current member delegates its verified connector admission."""
        if not self.registry_authoritative:
            raise AccessMeshError("mesh_not_primary")
        if (
            not isinstance(payload, dict)
            or set(payload) != {"namespace_id", "operation", "arguments", "actor", "connection_key"}
            or payload["namespace_id"] != self.store.namespace_id
        ):
            raise AccessMeshError("mesh_namespace_mismatch")
        if authenticated_peer_id not in self.store.trusted_issuers:
            raise AccessMeshError("access_mesh_untrusted_peer")
        raw = payload["actor"]
        binding = payload["connection_key"]
        if (
            not isinstance(raw, dict)
            or set(raw) != _ACTOR_FIELDS
            or not isinstance(payload["arguments"], dict)
            or not isinstance(binding, str)
            or len(binding) != 64
            or any(c not in "0123456789abcdef" for c in binding)
        ):
            raise AccessMeshError("invalid_request")
        try:
            actor = ActorContext(**raw)
        except (TypeError, ValueError) as exc:
            raise AccessMeshError("invalid_request") from exc
        token = REGISTRY_BINDING.set(binding)
        try:
            operation, arguments = payload["operation"], payload["arguments"]
            if operation == "session":
                if actor.endpoint_role != "access" or set(arguments) != {"action"}:
                    raise AccessMeshError("access_denied")
                return await self.issuer_session(actor, **arguments)
            from terminal_mcp.application.access_mesh_operator import AccessMeshOperator

            operator = AccessMeshOperator(self)
            handlers = {
                "slots": operator.slots,
                "get": operator.get,
                "defaults": operator.defaults,
                "mutate": operator.mutate,
            }
            if operation not in handlers:
                raise AccessMeshError("invalid_request")
            return await handlers[operation](actor, **arguments)
        finally:
            REGISTRY_BINDING.reset(token)

    async def begin_topology_transition(self, operation_id):
        # Commit-time guards close races with thread-backed issuer and attach
        # transactions. In-process locks alone do not survive a process crash.
        async with self._issuer_session_lock, self._issuer_lock:
            await asyncio.to_thread(self.store.begin_topology_transition, operation_id)

    async def abort_topology_transition(self, operation_id):
        await asyncio.to_thread(self.store.abort_topology_transition, operation_id)

    async def finish_topology_transition(self, operation_id, **kwargs):
        async with self._issuer_session_lock, self._issuer_lock:
            await asyncio.to_thread(self.store.finish_topology_transition, operation_id, **kwargs)
            if kwargs.get("clear_local", True):
                self._maintenance_snapshot = None
                self._scan_after = ""
                self.defaults = self.configured_defaults
                if self.replication is not None:
                    self.replication.reset_proof_evidence()

    @asynccontextmanager
    async def topology_guard(self, operation_id):
        await self.begin_topology_transition(operation_id)
        try:
            yield
        except BaseException:
            await self.abort_topology_transition(operation_id)
            raise

    async def export_registry(self):
        return await asyncio.to_thread(self.store.export_registry)

    async def import_registry(self, snapshot, **kwargs):
        return await asyncio.to_thread(self.store.import_registry, snapshot, **kwargs)
