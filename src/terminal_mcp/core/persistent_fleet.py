from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
from dataclasses import asdict, dataclass
from datetime import timedelta

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from terminal_mcp.core.orchestration import parse_utc, utc_now, utc_text
from terminal_mcp.storage.persistent_agents import PersistentStoreError


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class PersistentCommandPermit:
    logical_agent_id: str
    work_session_id: str
    session_epoch: int
    authority_node_id: str
    authority_epoch: int
    node_attachment_id: str
    node_instance_id: str
    scope: str
    issued_at: str
    permit_expires_at: str
    hard_expires_at: str
    slot_revision: int
    principal_id: str
    operation: str = "run"
    gate_revision: int = 1
    ttl_ms: int = 10000
    signature: str = ""

    def unsigned_dict(self) -> dict:
        payload = asdict(self)
        payload.pop("signature", None)
        return payload

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.unsigned_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> PersistentCommandPermit:
        return cls(**payload)


def sign_permit(permit: PersistentCommandPermit, private_key: str) -> PersistentCommandPermit:
    signer = Ed25519PrivateKey.from_private_bytes(_decode(private_key))
    signature = _encode(signer.sign(permit.canonical_bytes()))
    return PersistentCommandPermit(**permit.unsigned_dict(), signature=signature)


def verify_permit(permit: PersistentCommandPermit, public_key: str) -> bool:
    verifier = Ed25519PublicKey.from_public_bytes(_decode(public_key))
    try:
        verifier.verify(_decode(permit.signature), permit.canonical_bytes())
    except (InvalidSignature, ValueError):
        return False
    return True


class PersistentFleetBridge:
    """Minimal NodeAttachment + signed CommandAdmissionPermit bridge for M3.5."""

    def __init__(
        self,
        config,
        store,
        repo,
        terminal,
        task_store,
        *,
        client_factory=None,
        permit_ttl_ms: int = 10000,
        control_store=None,
        control_node_id: str | None = None,
    ):
        self.config = config
        self.store = store
        self.repo = repo
        self.terminal = terminal
        self.task_store = task_store
        self.client_factory = client_factory or self._default_client
        self.permit_ttl_ms = max(1, min(int(permit_ttl_ms), 60000))
        self.control_store = control_store
        self.control_node_id = control_node_id
        self.execution_fence = None
        self._permit_deadlines: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    def _default_client(self):
        return httpx.AsyncClient(timeout=self.config.request_timeout_seconds)

    def _headers(self, peer) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {peer.auth_token}",
            "X-Terminal-MCP-Peer": self.config.instance_id,
        }

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="persistent-fleet-reconciler")

    async def stop(self) -> None:
        self._stopped.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.sleep(1.0)
                await self.reconcile_remote_expiry()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)

    async def issue_permit(
        self,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        requesting_instance_id: str,
        scope: str,
        principal_id: str,
        operation: str | None = None,
        request_id: str | None = None,
    ) -> PersistentCommandPermit:
        if scope not in {"run", "cancel"}:
            raise PersistentStoreError("policy_incompatible")
        if requesting_instance_id not in self.config.peers_by_id:
            raise PersistentStoreError("authority_unavailable")
        operation = str(operation or scope).strip() or scope
        session = await self.store.assert_session_authority(
            logical_agent_id, work_session_id, session_epoch
        )
        if session.authority_node_id != self.config.instance_id:
            raise PersistentStoreError(
                "wrong_authority",
                blockers=[
                    {
                        "authority_node_id": session.authority_node_id,
                        "authority_epoch": session.authority_epoch,
                    }
                ],
            )
        if session.auth_principal_id and session.auth_principal_id != principal_id:
            raise PersistentStoreError("persistent_auth_required")
        slot = await self.store.get_slot(logical_agent_id)
        if (
            slot is None
            or slot.state != "active"
            or slot.authority_epoch != session.authority_epoch
        ):
            raise PersistentStoreError("session_not_active")
        gate = await self.store.fleet_gate(logical_agent_id)
        if gate["blocked"] and scope == "run":
            raise PersistentStoreError(
                "coordination_blocked",
                blockers=await self.store.open_message_obligations(logical_agent_id),
            )
        now = utc_now()
        hard_expiry = parse_utc(session.hard_expires_at)
        if now >= hard_expiry:
            raise PersistentStoreError("session_expired")
        request_payload = {
            "logical_agent_id": logical_agent_id,
            "work_session_id": work_session_id,
            "session_epoch": int(session_epoch),
            "requesting_instance_id": requesting_instance_id,
            "scope": scope,
            "operation": operation,
            "principal_id": principal_id,
            "gate_revision": int(gate["gate_revision"]),
        }
        fingerprint = self.store.idempotency_fingerprint(request_payload)
        if request_id:
            replay = await self.store.fleet_request_reserve(
                requesting_instance_id,
                operation,
                request_id,
                fingerprint,
                session.hard_expires_at,
            )
            if replay is not None:
                return PersistentCommandPermit.from_dict(replay)
        try:
            attachment = await self.store.record_node_attachment(
                node_attachment_id="att_" + secrets.token_urlsafe(12),
                logical_agent_id=logical_agent_id,
                work_session_id=work_session_id,
                session_epoch=session_epoch,
                node_instance_id=requesting_instance_id,
                authority_epoch=session.authority_epoch,
                hard_expires_at=session.hard_expires_at,
            )
            remaining_ms = max(1, int((hard_expiry - now).total_seconds() * 1000))
            ttl_ms = min(self.permit_ttl_ms, remaining_ms)
            permit_expiry = min(now + timedelta(milliseconds=ttl_ms), hard_expiry)
            permit = PersistentCommandPermit(
                logical_agent_id=logical_agent_id,
                work_session_id=work_session_id,
                session_epoch=session_epoch,
                authority_node_id=self.config.instance_id,
                authority_epoch=session.authority_epoch,
                node_attachment_id=attachment["node_attachment_id"],
                node_instance_id=requesting_instance_id,
                scope=scope,
                issued_at=utc_text(now),
                permit_expires_at=utc_text(permit_expiry),
                hard_expires_at=session.hard_expires_at,
                slot_revision=slot.slot_revision,
                principal_id=principal_id,
                operation=operation,
                gate_revision=int(gate["gate_revision"]),
                ttl_ms=ttl_ms,
            )
            signed = sign_permit(permit, self.config.signing_private_key)
            if request_id:
                await self.store.fleet_request_complete(
                    requesting_instance_id,
                    operation,
                    request_id,
                    fingerprint,
                    signed.as_dict(),
                )
            return signed
        except Exception:
            if request_id:
                await self.store.fleet_request_abort(
                    requesting_instance_id, operation, request_id, fingerprint
                )
            raise

    async def acquire_permit(
        self,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        scope: str,
        principal_id: str,
        operation: str | None = None,
        request_id: str | None = None,
    ) -> PersistentCommandPermit:
        operation = str(operation or scope).strip() or scope
        request_id = request_id or ("fr_" + secrets.token_urlsafe(12))
        request_started = time.monotonic()
        payload = {
            "logical_agent_id": logical_agent_id,
            "work_session_id": work_session_id,
            "session_epoch": session_epoch,
            "requesting_instance_id": self.config.instance_id,
            "scope": scope,
            "operation": operation,
            "request_id": request_id,
            "principal_id": principal_id,
        }
        async with self.client_factory() as client:
            for peer in self.config.peers:
                try:
                    response = await client.post(
                        f"{peer.origin}/internal/fleet/persistent/permit",
                        headers=self._headers(peer),
                        json=payload,
                    )
                    if response.status_code == 404:
                        continue
                    response.raise_for_status()
                    permit = PersistentCommandPermit.from_dict(response.json()["permit"])
                    ttl_ms = int(permit.ttl_ms)
                    deadline = request_started + min(ttl_ms, self.permit_ttl_ms) / 1000.0
                    valid = (
                        permit.authority_node_id == peer.instance_id
                        and permit.node_instance_id == self.config.instance_id
                        and permit.logical_agent_id == logical_agent_id
                        and permit.work_session_id == work_session_id
                        and permit.session_epoch == session_epoch
                        and permit.scope == scope
                        and permit.operation == operation
                        and permit.principal_id == principal_id
                        and permit.gate_revision > 0
                        and 0 < ttl_ms <= self.permit_ttl_ms
                        and verify_permit(permit, peer.public_key)
                        and utc_now() < parse_utc(permit.permit_expires_at)
                        and utc_now() < parse_utc(permit.hard_expires_at)
                        and time.monotonic() < deadline
                    )
                    if valid:
                        self._permit_deadlines[permit.signature] = deadline
                        return permit
                except Exception:
                    continue
        raise PersistentStoreError("authority_unavailable")

    def ensure_permit_valid(self, permit: PersistentCommandPermit) -> None:
        deadline = self._permit_deadlines.get(permit.signature)
        if deadline is None or time.monotonic() >= deadline:
            raise PersistentStoreError("permit_expired")
        if utc_now() >= parse_utc(permit.hard_expires_at):
            raise PersistentStoreError("session_expired")

    async def revoke_session(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        reason: str,
    ) -> list[dict]:
        attachments = await self.store.attachments_for_session(
            logical_agent_id, work_session_id, session_epoch, active_only=True
        )
        blockers = []
        if not attachments:
            return blockers
        async with self.client_factory() as client:
            for attachment in attachments:
                peer = self.config.peers_by_id.get(attachment["node_instance_id"])
                if peer is None:
                    blockers.append(
                        {
                            "kind": "authority_unreachable",
                            "node_instance_id": attachment["node_instance_id"],
                        }
                    )
                    continue
                try:
                    response = await client.post(
                        f"{peer.origin}/internal/fleet/persistent/revoke",
                        headers=self._headers(peer),
                        json={
                            "logical_agent_id": logical_agent_id,
                            "work_session_id": work_session_id,
                            "session_epoch": session_epoch,
                            "authority_node_id": self.config.instance_id,
                            "reason": reason,
                        },
                    )
                    response.raise_for_status()
                    remote = response.json().get("blockers") or []
                    if remote:
                        blockers.extend(remote)
                    else:
                        await self.store.revoke_node_attachment(attachment["node_attachment_id"])
                except Exception:
                    blockers.append(
                        {"kind": "authority_unreachable", "node_instance_id": peer.instance_id}
                    )
        return blockers

    async def receive_revoke(
        self,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        reason: str,
    ) -> list[dict]:
        await self.store.revoke_command_permits(logical_agent_id, work_session_id, session_epoch)
        if self.execution_fence is None:
            return [{"kind": "runtime_unavailable", "node_instance_id": self.config.instance_id}]
        return await self.execution_fence.revoke_session(
            logical_agent_id, work_session_id, session_epoch, reason=reason
        )

    async def reconcile_remote_expiry(self) -> None:
        if self.execution_fence is None:
            return
        for session in await self.store.expired_remote_permit_sessions():
            await self.store.revoke_command_permits(
                session["logical_agent_id"],
                session["work_session_id"],
                session["session_epoch"],
            )
            await self.execution_fence.revoke_session(
                session["logical_agent_id"],
                session["work_session_id"],
                session["session_epoch"],
                reason="hard_duration",
            )
