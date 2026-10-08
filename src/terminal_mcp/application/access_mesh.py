"""Local Access Mesh admission and durable lifecycle reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import sqlite3
from datetime import UTC, datetime

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.access_mesh_grants import AccessMeshError, AccessSlotEvent, SlotPolicy
from terminal_mcp.core.managed_sessions import (
    FINALIZATION_OPERATIONS,
    READ_OPERATIONS,
    ManagedOperation,
)
from terminal_mcp.core.persistent_admission import PersistentAdmissionError
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.core.provider_identity import ProviderIdentityError, ProviderIdentityRegistry
from terminal_mcp.storage.access_mesh import AccessMeshStore, public_name

_LOG = logging.getLogger(__name__)


class AccessMeshApplication:
    def __init__(
        self,
        store: AccessMeshStore,
        *,
        task_store,
        execution_fence,
        legacy_enabled: bool = True,
        defaults: SlotPolicy | None = None,
        clock=None,
    ):
        self.store = store
        self.task_store = task_store
        self.execution_fence = execution_fence
        self.legacy_enabled = legacy_enabled
        self.defaults = defaults or SlotPolicy(1200, 60, True, 120, 30)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.registry = ProviderIdentityRegistry()
        self._cleanup_lock = asyncio.Lock()
        self._issuer_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._scan_after = ""
        self.last_error: str | None = None

    @staticmethod
    def require_write(actor: ActorContext) -> None:
        try:
            admission = actor.admission()
            if admission is None:
                raise AccessMeshError("persistent_auth_required")
            admission.require("terminal:execute")
        except PersistentAdmissionError as exc:
            raise AccessMeshError(exc.code) from exc

    def connection_key(self, actor: ActorContext) -> str:
        """Index trusted caller evidence, never an agent-provided identity field."""
        if not actor.principal_id or not actor.provider or not actor.provider_metadata:
            raise AccessMeshError("identity_metadata_missing")
        try:
            identity = self.registry.resolve(actor.provider, actor.provider_metadata)
        except ProviderIdentityError as exc:
            raise AccessMeshError(exc.code) from exc
        scope = [
            "access-mesh-binding-v2",
            self.store.local_node_id,
            actor.endpoint_role,
            actor.principal_id,
            identity.binding_key,
        ]
        return hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()

    async def attach(
        self, actor: ActorContext, *, issuer_node_id: str | None, access_code: str
    ) -> dict:
        self.require_write(actor)
        if isinstance(access_code, str) and ":" in access_code:
            prefix, access_code = access_code.split(":", 1)
            if issuer_node_id and issuer_node_id != prefix:
                raise AccessMeshError("access_mesh_issuer_mismatch")
            issuer_node_id = prefix
        if not issuer_node_id:
            raise AccessMeshError("access_mesh_issuer_required")
        slot = await asyncio.to_thread(
            self.store.attach,
            issuer_id=issuer_node_id,
            code=access_code,
            connection_key=self.connection_key(actor),
        )
        await self.cleanup()
        identity = await asyncio.to_thread(self.store.local_identity, slot, now=self.clock())
        return {
            **identity,
            "action": "attach",
            "attached": True,
            "role": actor.endpoint_role,
            "contract_version": actor.contract_version,
        }

    async def resolve(self, actor: ActorContext, operation: ManagedOperation) -> dict:
        if operation not in READ_OPERATIONS:
            self.require_write(actor)
        slot = await asyncio.to_thread(self.store.attached_slot, self.connection_key(actor))
        if slot is None:
            raise AccessMeshError("session_attach_required")
        identity = await asyncio.to_thread(self.store.local_identity, slot, now=self.clock())
        # Cleanup can be pending after an expiry first observed by this request.
        # It is local and bounded; remote issuer availability is never involved.
        if identity["cleanup_pending"]:
            await self.cleanup()
            identity = await asyncio.to_thread(self.store.local_identity, slot, now=self.clock())
        if operation in READ_OPERATIONS:
            return identity
        if identity["cleanup_pending"]:
            raise AccessMeshError("access_mesh_cleanup_pending")
        if "work_session_id" not in identity:
            phase = identity["session_lifecycle"]["state"]
            raise AccessMeshError("window_cooldown" if phase == "cooldown" else "session_expired")
        if (
            identity["session_lifecycle"]["state"] == "draining"
            and operation not in FINALIZATION_OPERATIONS
        ):
            raise AccessMeshError("session_draining")
        return identity

    async def cleanup(self) -> None:
        async with self._cleanup_lock:
            jobs = await asyncio.to_thread(self.store.pending_cleanup)
            for job in jobs:
                error = None
                try:
                    async with asyncio.timeout(3):
                        if job["work_session_id"]:
                            blockers = await self.execution_fence.revoke_session(
                                job["logical_agent_id"],
                                job["work_session_id"],
                                job["session_epoch"],
                                reason=job["reason"],
                            )
                            if blockers:
                                raise AccessMeshError("access_mesh_execution_cleanup_pending")
                        if job["release_claims"]:
                            result = await self.task_store.release_owner_claims_mutation(
                                owner=ClaimOwner.logical_agent(job["logical_agent_id"]),
                                reason=job["reason"],
                            )
                            if not result["ok"]:
                                raise AccessMeshError("access_mesh_claim_cleanup_pending")
                except Exception as exc:
                    error = getattr(exc, "code", type(exc).__name__)
                await asyncio.to_thread(self.store.finish_cleanup, job["job_id"], error=error)

    async def tick(self) -> None:
        slots = await asyncio.to_thread(self.store.local_slots, after=self._scan_after)
        for slot in slots:
            try:
                await asyncio.to_thread(self.store.local_identity, slot, now=self.clock())
            except Exception as exc:
                self.last_error = getattr(exc, "code", type(exc).__name__)
                _LOG.warning("Access Mesh cycle reconciliation failed: %s", self.last_error)
        self._scan_after = slots[-1].logical_agent_id if len(slots) == 50 else ""
        await self.cleanup()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        await self.tick()
        self._task = asyncio.create_task(self._loop(), name="access-mesh-local-lifecycle")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except TimeoutError:
                try:
                    await self.tick()
                except Exception as exc:
                    self.last_error = getattr(exc, "code", type(exc).__name__)
                    _LOG.warning("Access Mesh lifecycle tick failed: %s", self.last_error)

    async def issue(
        self,
        actor: ActorContext,
        *,
        kind: str = "legacy",
        policy: SlotPolicy | None = None,
        start: bool = True,
        code: str | None = None,
    ) -> dict:
        self.require_write(actor)
        if kind not in {"legacy", "persistent"}:
            raise AccessMeshError("invalid_mode")
        if kind == "legacy" and not self.legacy_enabled:
            raise AccessMeshError("legacy_disabled")
        async with self._issuer_lock:
            for _ in range(100):
                allocated = code or f"{secrets.randbelow(10000):04d}"
                slot_id = "as_" + secrets.token_urlsafe(18)
                agent_id = f"la_{self.store.local_node_id}_" + secrets.token_urlsafe(18)
                event = AccessSlotEvent(
                    issuer_id=self.store.local_node_id,
                    slot_id=slot_id,
                    logical_agent_id=agent_id,
                    revision=1,
                    event_id="ae_" + secrets.token_urlsafe(18),
                    kind="SlotIssued",
                    policy=policy or self.defaults,
                    code_tag=self.store.code_tag(self.store.local_node_id, allocated),
                    effective_at=self.clock() if start else None,
                )
                try:
                    await asyncio.to_thread(
                        self.store.apply_event,
                        event,
                        authenticated_peer_id=self.store.local_node_id,
                        issued_kind=kind,
                        broadcast_to=tuple(
                            sorted(self.store.trusted_issuers - {self.store.local_node_id})
                        ),
                    )
                    break
                except sqlite3.IntegrityError:
                    if code is not None:
                        raise AccessMeshError("access_mesh_code_in_use") from None
            else:
                raise AccessMeshError("access_mesh_code_capacity")
        return {
            "ok": True,
            "action": "start" if start else "create",
            "issuer_node_id": event.issuer_id,
            "slot_id": slot_id,
            "logical_agent_id": agent_id,
            "mode": kind,
            "revision": 1,
            "public_name": public_name(event.issuer_id, agent_id),
            "access_code": allocated,
        }

    async def change(
        self,
        actor: ActorContext,
        *,
        slot_id: str,
        kind: str,
        expected_revision: int,
        policy: SlotPolicy | None = None,
        effective_at: datetime | None = None,
    ) -> dict:
        self.require_write(actor)
        async with self._issuer_lock:
            slot = await asyncio.to_thread(self.store.slot, self.store.local_node_id, slot_id)
            if slot is None:
                raise AccessMeshError("access_mesh_slot_not_found")
            if slot.revision != expected_revision:
                raise AccessMeshError("revision_conflict")
            code = f"{secrets.randbelow(10000):04d}" if kind == "AccessCodeRotated" else None
            event = AccessSlotEvent(
                issuer_id=slot.issuer_id,
                slot_id=slot.slot_id,
                logical_agent_id=slot.logical_agent_id,
                revision=slot.revision + 1,
                event_id="ae_" + secrets.token_urlsafe(18),
                kind=kind,
                policy=policy,
                code_tag=self.store.code_tag(slot.issuer_id, code) if code else None,
                effective_at=effective_at or (self.clock() if kind.startswith("Session") else None),
            )
            try:
                await asyncio.to_thread(
                    self.store.apply_event,
                    event,
                    authenticated_peer_id=self.store.local_node_id,
                    broadcast_to=tuple(
                        sorted(self.store.trusted_issuers - {self.store.local_node_id})
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AccessMeshError("access_mesh_code_in_use") from exc
        await self.cleanup()
        return {
            "ok": True,
            "issuer_node_id": slot.issuer_id,
            "slot_id": slot.slot_id,
            "revision": event.revision,
            **({"access_code": code} if code else {}),
        }
