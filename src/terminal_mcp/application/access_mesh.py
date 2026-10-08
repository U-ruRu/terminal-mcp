"""Local Access Mesh admission and durable lifecycle reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

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
from terminal_mcp.storage.access_mesh import AccessMeshStore, local_cycle, public_name

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
        self.store.clock = self.clock
        stored_defaults = self.store.defaults()
        if stored_defaults is not None:
            self.defaults = SlotPolicy(**stored_defaults["policy"])
            self.legacy_enabled = stored_defaults["legacy_enabled"]
        self.registry = ProviderIdentityRegistry()
        self._cleanup_lock = asyncio.Lock()
        self._issuer_lock = asyncio.Lock()
        self._issuer_session_lock = asyncio.Lock()
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
        await self.cleanup(logical_agent_id=slot.logical_agent_id, budget_seconds=0.5)
        identity = await asyncio.to_thread(self.store.local_identity, slot, now=self.clock())
        return {
            **identity,
            "action": "attach",
            "attached": True,
            "role": actor.endpoint_role,
            "contract_version": actor.contract_version,
        }

    async def observe(self, actor: ActorContext) -> dict:
        try:
            key = self.connection_key(actor)
            slot = await asyncio.to_thread(self.store.attached_slot, key)
        except AccessMeshError as exc:
            if exc.code not in {"identity_metadata_missing", "identity_metadata_invalid"}:
                raise
            slot = None
        if slot is None:
            return {
                "ok": True,
                "action": "status",
                "attached": False,
                "authority_node_id": self.store.local_node_id,
                "role": actor.endpoint_role,
                "contract_version": actor.contract_version,
            }
        identity = await asyncio.to_thread(self.store.observed_identity, slot, now=self.clock())
        return {
            **identity,
            "action": "status",
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
        if operation in READ_OPERATIONS:
            return await asyncio.to_thread(self.store.observed_identity, slot, now=self.clock())
        identity = await asyncio.to_thread(self.store.local_identity, slot, now=self.clock())
        # Cleanup can be pending after an expiry first observed by this request.
        # It is local and bounded; remote issuer availability is never involved.
        if identity["cleanup_pending"]:
            await self.cleanup(logical_agent_id=slot.logical_agent_id, budget_seconds=0.5)
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

    async def cleanup(
        self, *, logical_agent_id: str | None = None, budget_seconds: float = 2.0
    ) -> None:
        if self._cleanup_lock.locked():
            return
        async with self._cleanup_lock:
            deadline = asyncio.get_running_loop().time() + budget_seconds
            jobs = await asyncio.to_thread(
                self.store.pending_cleanup, logical_agent_id=logical_agent_id
            )
            for job in jobs:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                error = None
                try:
                    async with asyncio.timeout(min(1.0, remaining)):
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

    async def health_components(self) -> dict:
        pending = await asyncio.to_thread(self.store.pending_cleanup, limit=1)
        running = self._task is not None and not self._task.done()
        status = (
            "failed" if not running else "degraded" if pending or self.last_error else "healthy"
        )
        reason = (
            "lifecycle_worker_stopped"
            if not running
            else "cleanup_pending"
            if pending
            else self.last_error
        )
        return {
            "access_mesh": {
                "status": status,
                "ok": status == "healthy",
                **({"reason": reason} if reason else {}),
            }
        }

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        await asyncio.to_thread(self.store.initialize_command_journal)
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

    def _receipt_spec(self, actor, payload):
        binding = self.connection_key(actor)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        # JSON-RPC request ids are transport correlation only. Separate
        # operator requests remain separate even when the connector reuses id.
        key = hashlib.sha256(
            f"{binding}:{secrets.token_urlsafe(24)}:{fingerprint}".encode()
        ).hexdigest()
        return {"request_key": key, "fingerprint": fingerprint, "connection_key": binding}

    @staticmethod
    def _slot_receipt(slot, action, *, revision=None, code=None):
        return {
            "ok": True,
            "action": action,
            "issuer_node_id": slot.issuer_id,
            "slot_id": slot.slot_id,
            "logical_agent_id": slot.logical_agent_id,
            "public_name": public_name(slot.issuer_id, slot.logical_agent_id),
            "mode": slot.kind,
            "revision": revision or slot.revision,
            **({"access_code": code} if code is not None else {}),
        }

    async def issue(
        self,
        actor: ActorContext,
        *,
        kind: str = "legacy",
        policy: SlotPolicy | None = None,
        start: bool = True,
        code: str | None = None,
        receipt_spec=None,
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
                result = {
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
                receipt = (
                    self.store.prepare_receipt(
                        event_id=event.event_id, result=result, **receipt_spec
                    )
                    if receipt_spec
                    else None
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
                        mutation_receipt=receipt,
                    )
                    return result
                except AccessMeshError as exc:
                    if exc.code != "access_mesh_code_in_use":
                        raise
                    if code is not None:
                        raise AccessMeshError("access_mesh_code_in_use") from None
            raise AccessMeshError("access_mesh_code_capacity")

    async def change(
        self,
        actor: ActorContext,
        *,
        slot_id: str,
        kind: str,
        expected_revision: int,
        policy: SlotPolicy | None = None,
        effective_at: datetime | None = None,
        deadline_at: datetime | None = None,
        receipt_spec=None,
    ) -> dict:
        self.require_write(actor)
        async with self._issuer_lock:
            slot = await asyncio.to_thread(self.store.slot, self.store.local_node_id, slot_id)
            if slot is None:
                raise AccessMeshError("access_mesh_slot_not_found")
            if slot.revision != expected_revision:
                raise AccessMeshError("revision_conflict")
            code = None
            if kind == "AccessCodeRotated":
                for _ in range(100):
                    candidate = f"{secrets.randbelow(10000):04d}"
                    if (
                        await asyncio.to_thread(self.store.code_slot, slot.issuer_id, candidate)
                        is None
                    ):
                        code = candidate
                        break
                if code is None:
                    raise AccessMeshError("access_mesh_code_capacity")
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
                deadline_at=deadline_at,
            )
            action = {
                "SessionStarted": "start",
                "SessionEnded": "end",
                "SessionUpdated": "update",
                "SlotPolicyChanged": "policy",
                "SlotSuspended": "suspend",
                "SlotResumed": "resume",
                "SlotDeleted": "delete",
                "AccessCodeRotated": "rotate",
            }.get(kind, "update")
            result = self._slot_receipt(slot, action, revision=event.revision, code=code)
            receipt = (
                self.store.prepare_receipt(event_id=event.event_id, result=result, **receipt_spec)
                if receipt_spec
                else None
            )
            try:
                await asyncio.to_thread(
                    self.store.apply_event,
                    event,
                    authenticated_peer_id=self.store.local_node_id,
                    broadcast_to=tuple(
                        sorted(self.store.trusted_issuers - {self.store.local_node_id})
                    ),
                    mutation_receipt=receipt,
                )
            except AccessMeshError:
                raise
        # Cleanup is resumable after commit. Its failures cannot turn a durable
        # issuer event into an apparent pre-commit failure or permit duplicate events.
        try:
            await self.cleanup()
        except Exception:
            _LOG.exception("Access Mesh cleanup pending after committed issuer event")
        return result

    async def issuer_session(
        self, actor: ActorContext, *, action: str, mode=None, code=None
    ) -> dict:
        if action not in {"start", "end", "status"}:
            raise AccessMeshError("invalid_request")
        if action != "status":
            self.require_write(actor)
        binding = self.connection_key(actor)
        async with self._issuer_session_lock:
            spec = self._receipt_spec(actor, {"action": action, "mode": mode, "code": code})
            if action != "status":
                replay = await asyncio.to_thread(
                    self.store.receipt, spec["request_key"], spec["fingerprint"]
                )
                if replay is not None:
                    return replay
            if action == "start" and mode == "legacy":
                if code is not None:
                    raise AccessMeshError("legacy_code_not_allowed")
                return await self.issue(actor, kind="legacy", receipt_spec=spec)
            slot = (
                await asyncio.to_thread(self.store.code_slot, self.store.local_node_id, code)
                if code is not None
                else await asyncio.to_thread(self.store.issuer_bound_slot, binding)
            )
            if slot is None or slot.state == "deleted":
                raise AccessMeshError("access_mesh_slot_not_found")
            cycle = local_cycle(slot, self.clock())
            if action == "status":
                return {
                    **self._slot_receipt(slot, action),
                    "slot_state": slot.state,
                    "session_lifecycle": cycle,
                    "policy": asdict(slot.policy),
                }
            if action == "start":
                if mode != "persistent" or slot.kind != "persistent":
                    raise AccessMeshError("access_mode_mismatch")
                if slot.state != "active":
                    raise AccessMeshError("slot_not_armed")
                if cycle["state"] == "cooldown":
                    raise AccessMeshError("window_cooldown")
                if cycle["state"] in {"idle", "expired"}:
                    last_end = await asyncio.to_thread(self.store.last_session_end, slot.slot_id)
                    if last_end is not None and self.clock() < last_end + timedelta(
                        seconds=slot.policy.cooldown_seconds
                    ):
                        raise AccessMeshError("window_cooldown")
                    # Manual activation also respects the preceding fixed window's cooldown.
                    if slot.anchor is not None and self.clock() < slot.anchor + timedelta(
                        seconds=slot.policy.duration_seconds + slot.policy.cooldown_seconds
                    ):
                        raise AccessMeshError("window_cooldown")
                    return await self.change(
                        actor,
                        slot_id=slot.slot_id,
                        kind="SessionStarted",
                        expected_revision=slot.revision,
                        receipt_spec=spec,
                    )
            elif cycle["state"] in {"active", "warning", "draining"}:
                return await self.change(
                    actor,
                    slot_id=slot.slot_id,
                    kind="SessionEnded",
                    expected_revision=slot.revision,
                    receipt_spec=spec,
                )
            result = self._slot_receipt(slot, action)
            receipt = self.store.prepare_receipt(event_id="none", result=result, **spec)
            await asyncio.to_thread(self.store.save_receipt, receipt)
            return result
