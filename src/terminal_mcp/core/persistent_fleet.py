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
        access_authority=None,
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
        self.access_authority = access_authority
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

    def _access_control_node_id(self) -> str:
        return (self.control_node_id or self.config.instance_id).strip()

    async def _remote_access_call(self, operation: str, payload: dict) -> dict:
        control_id = self._access_control_node_id()
        peer = self.config.peers_by_id.get(control_id)
        if peer is None:
            raise PersistentStoreError("authority_unavailable")
        body = {**payload, "requesting_instance_id": self.config.instance_id}
        async with self.client_factory() as client:
            response = await client.post(
                f"{peer.origin}/internal/fleet/persistent/access/{operation}",
                headers=self._headers(peer),
                json=body,
            )
        if response.status_code >= 400:
            raise PersistentStoreError("authority_unavailable")
        data = response.json()
        if not data.get("ok"):
            raise PersistentStoreError(str(data.get("code") or "authority_unavailable"))
        return data

    async def resolve_access_code(self, access_code: str) -> dict:
        control_id = self._access_control_node_id()
        if control_id == self.config.instance_id:
            if self.access_authority is None:
                raise PersistentStoreError("authority_unavailable")
            result = await self.access_authority.resolve_access_code(access_code)
            if result is None:
                raise PersistentStoreError("access_denied")
            return result
        data = await self._remote_access_call("resolve", {"access_code": access_code})
        return dict(data["access"])

    async def get_access_slot(self, logical_agent_id: str) -> dict | None:
        control_id = self._access_control_node_id()
        if control_id != self.config.instance_id:
            data = await self._remote_access_call("get", {"logical_agent_id": logical_agent_id})
            return dict(data["access"]) if data.get("access") is not None else None
        if self.access_authority is None:
            raise PersistentStoreError("authority_unavailable")
        return await self.access_authority.access_slot(logical_agent_id)

    async def update_access_display_suffix(
        self, logical_agent_id: str, display_suffix: str | None
    ) -> dict:
        control_id = self._access_control_node_id()
        if control_id != self.config.instance_id:
            data = await self._remote_access_call(
                "display",
                {"logical_agent_id": logical_agent_id, "display_suffix": display_suffix},
            )
            return dict(data["access"])
        if self.access_authority is None:
            raise PersistentStoreError("authority_unavailable")
        return await self.access_authority.update_access_display_suffix(
            logical_agent_id, display_suffix
        )

    async def ensure_access_slot(
        self,
        logical_agent_id: str,
        authority_node_id: str,
        *,
        display_suffix: str | None = None,
        forbidden_codes=(),
    ) -> dict:
        control_id = self._access_control_node_id()
        payload = {
            "logical_agent_id": logical_agent_id,
            "authority_node_id": authority_node_id,
            "slot_kind": "persistent",
            "display_suffix": display_suffix,
            "forbidden_codes": list(forbidden_codes),
        }
        if control_id != self.config.instance_id:
            data = await self._remote_access_call("register", payload)
            return dict(data["access"])
        if self.access_authority is None:
            raise PersistentStoreError("authority_unavailable")
        await self.access_authority.reserve_access_codes(payload["forbidden_codes"])
        slot = await self.access_authority.register_access_slot(
            logical_agent_id,
            authority_node_id,
            slot_kind="persistent",
            display_suffix=display_suffix,
        )
        if int(slot["access_generation"]) == 0:
            issued = await self.access_authority.issue_access_code(logical_agent_id)
            return {**slot, **issued}
        return slot

    async def rotate_access_code(self, logical_agent_id: str, *, forbidden_codes=()) -> dict:
        control_id = self._access_control_node_id()
        payload = {"logical_agent_id": logical_agent_id, "forbidden_codes": list(forbidden_codes)}
        if control_id != self.config.instance_id:
            data = await self._remote_access_call("rotate", payload)
            return dict(data["access"])
        if self.access_authority is None:
            raise PersistentStoreError("authority_unavailable")
        await self.access_authority.reserve_access_codes(payload["forbidden_codes"])
        return await self.access_authority.issue_access_code(logical_agent_id)

    async def retire_access_slot(self, logical_agent_id: str) -> dict:
        control_id = self._access_control_node_id()
        if control_id != self.config.instance_id:
            data = await self._remote_access_call("retire", {"logical_agent_id": logical_agent_id})
            return dict(data["access"])
        if self.access_authority is None:
            raise PersistentStoreError("authority_unavailable")
        return await self.access_authority.retire_access_slot(logical_agent_id)

    async def route_info(self, logical_agent_id: str) -> dict | None:
        if self.control_store is None:
            return None
        return await self.control_store.route(logical_agent_id)

    async def publish_authority(
        self,
        logical_agent_id: str,
        authority_node_id: str,
        authority_epoch: int,
    ) -> dict | None:
        if self.control_store is None:
            return None
        return await self.control_store.publish_route(
            logical_agent_id,
            authority_node_id,
            authority_epoch,
        )

    @staticmethod
    def _wrong_authority_blocker(route: dict) -> dict:
        return {
            "authority_node_id": route["authority_node_id"],
            "authority_epoch": int(route["authority_epoch"]),
            "routing_revision": int(route.get("routing_revision") or 0),
        }

    async def _guard_local_authority(self, logical_agent_id: str) -> dict | None:
        route = await self.route_info(logical_agent_id)
        if route is None:
            return None
        if route["state"] == "recovery_required":
            raise PersistentStoreError(
                "recovery_required",
                blockers=[self._wrong_authority_blocker(route)],
            )
        if route["authority_node_id"] != self.config.instance_id:
            raise PersistentStoreError(
                "wrong_authority",
                blockers=[self._wrong_authority_blocker(route)],
            )
        return route

    async def materialize_session(
        self,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        requesting_instance_id: str,
    ) -> dict:
        if requesting_instance_id not in self.config.peers_by_id:
            raise PersistentStoreError("authority_unavailable")
        await self._guard_local_authority(logical_agent_id)
        session = await self.store.assert_session_authority(
            logical_agent_id,
            work_session_id,
            session_epoch,
        )
        if session.authority_node_id != self.config.instance_id:
            route = {
                "authority_node_id": session.authority_node_id,
                "authority_epoch": session.authority_epoch,
                "routing_revision": 0,
            }
            raise PersistentStoreError(
                "wrong_authority",
                blockers=[self._wrong_authority_blocker(route)],
            )
        attachment = await self.store.record_node_attachment(
            node_attachment_id="att_" + secrets.token_urlsafe(12),
            logical_agent_id=logical_agent_id,
            work_session_id=work_session_id,
            session_epoch=session_epoch,
            node_instance_id=requesting_instance_id,
            authority_epoch=session.authority_epoch,
            hard_expires_at=session.hard_expires_at,
        )
        route = await self.publish_authority(
            logical_agent_id,
            session.authority_node_id,
            session.authority_epoch,
        )
        return {
            "attachment": attachment,
            "route": route,
            "obligations": await self.store.open_message_obligations(logical_agent_id),
        }

    async def update_attachment_presence(
        self,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        requesting_instance_id: str,
        task_summary: str,
        intent: str,
        work_scope: list[str] | tuple[str, ...],
        details: list[str] | tuple[str, ...],
        current_step: int,
        intent_updated_at: str | None = None,
        last_activity_at: str | None = None,
    ) -> dict:
        if requesting_instance_id not in self.config.peers_by_id:
            raise PersistentStoreError("authority_unavailable")
        await self._guard_local_authority(logical_agent_id)
        session = await self.store.assert_session_authority(
            logical_agent_id, work_session_id, session_epoch
        )
        if session.authority_node_id != self.config.instance_id:
            route = await self.route_info(logical_agent_id) or {
                "authority_node_id": session.authority_node_id,
                "authority_epoch": session.authority_epoch,
                "routing_revision": 0,
            }
            raise PersistentStoreError(
                "wrong_authority",
                blockers=[self._wrong_authority_blocker(route)],
            )
        return await self.store.record_attachment_presence(
            logical_agent_id=logical_agent_id,
            work_session_id=work_session_id,
            session_epoch=session_epoch,
            node_instance_id=requesting_instance_id,
            task_summary=task_summary,
            intent=intent,
            work_scope=work_scope,
            details=details,
            current_step=current_step,
            intent_updated_at=intent_updated_at,
            last_activity_at=last_activity_at,
        )

    async def detach_session(
        self,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        requesting_instance_id: str,
    ) -> dict:
        if requesting_instance_id not in self.config.peers_by_id:
            raise PersistentStoreError("authority_unavailable")
        await self._guard_local_authority(logical_agent_id)
        attachments = await self.store.attachments_for_session(
            logical_agent_id,
            work_session_id,
            session_epoch,
            active_only=True,
        )
        detached = False
        for attachment in attachments:
            if attachment["node_instance_id"] != requesting_instance_id:
                continue
            detached = (
                await self.store.revoke_node_attachment(attachment["node_attachment_id"])
                or detached
            )
        return {
            "logical_agent_id": logical_agent_id,
            "work_session_id": work_session_id,
            "session_epoch": int(session_epoch),
            "node_instance_id": requesting_instance_id,
            "detached": detached,
        }

    async def create_obligation(
        self,
        *,
        logical_agent_id: str,
        sender_agent_id: str,
        text: str,
        require_reply: bool = False,
        alert: bool = False,
        message_ref: str | None = None,
    ) -> dict:
        await self._guard_local_authority(logical_agent_id)
        slot = await self.store.get_slot(logical_agent_id)
        if slot is None:
            raise PersistentStoreError("slot_not_found")
        if slot.authority_node_id != self.config.instance_id:
            route = await self.route_info(logical_agent_id) or {
                "authority_node_id": slot.authority_node_id,
                "authority_epoch": slot.authority_epoch,
                "routing_revision": 0,
            }
            raise PersistentStoreError(
                "wrong_authority",
                blockers=[self._wrong_authority_blocker(route)],
            )
        message_ref = message_ref or (f"{self.config.instance_id}:msg:{secrets.token_urlsafe(12)}")
        obligation = await self.store.create_message_obligation(
            message_ref=message_ref,
            logical_agent_id=logical_agent_id,
            sender_agent_id=sender_agent_id,
            text=text,
            require_reply=require_reply,
            alert=alert,
        )
        session = await self.store.active_session_for_slot(logical_agent_id)
        attachments = []
        if session is not None and session.state == "active":
            attachments = await self.store.attachments_for_session(
                logical_agent_id,
                session.work_session_id,
                session.session_epoch,
                active_only=True,
            )
        payload = {
            "home_node_id": self.config.instance_id,
            "logical_agent_id": logical_agent_id,
            "message_ref": message_ref,
            "sender_agent_id": sender_agent_id,
            "text": text,
            "require_reply": bool(require_reply),
            "alert": bool(alert),
            "gate_revision": int(obligation["gate_revision"]),
            "created_at": obligation["created_at"],
            "work_session_id": session.work_session_id if session else None,
            "session_epoch": session.session_epoch if session else None,
        }
        delivered_nodes: list[str] = []
        if attachments:
            async with self.client_factory() as client:
                for attachment in attachments:
                    peer = self.config.peers_by_id.get(attachment["node_instance_id"])
                    if peer is None:
                        continue
                    try:
                        response = await client.post(
                            f"{peer.origin}/internal/fleet/persistent/obligation-delivery",
                            headers=self._headers(peer),
                            json=payload,
                        )
                        response.raise_for_status()
                    except Exception:
                        continue
                    delivered_nodes.append(peer.instance_id)
        return {
            **obligation,
            "delivered_nodes": delivered_nodes,
        }

    async def receive_obligation_delivery(
        self,
        payload: dict,
        *,
        authenticated_home_node_id: str,
    ) -> dict:
        if authenticated_home_node_id not in self.config.peers_by_id:
            raise PersistentStoreError("authority_unavailable")
        home_node_id = str(payload.get("home_node_id") or "")
        if home_node_id != authenticated_home_node_id:
            raise PersistentStoreError("wrong_authority")
        message_ref = str(payload.get("message_ref") or "")
        logical_agent_id = str(payload.get("logical_agent_id") or "")
        if not message_ref or not logical_agent_id:
            raise PersistentStoreError("invalid_message")
        item = {
            "home_node_id": home_node_id,
            "logical_agent_id": logical_agent_id,
            "message_ref": message_ref,
            "sender_agent_id": str(payload.get("sender_agent_id") or ""),
            "text": str(payload.get("text") or ""),
            "require_reply": bool(payload.get("require_reply")),
            "alert": bool(payload.get("alert")),
            "gate_revision": int(payload.get("gate_revision") or 1),
            "created_at": str(payload.get("created_at") or ""),
            "work_session_id": payload.get("work_session_id"),
            "session_epoch": payload.get("session_epoch"),
        }
        existing = self._remote_obligations.get(message_ref)
        if existing is None or item["gate_revision"] >= int(existing["gate_revision"]):
            self._remote_obligations[message_ref] = item
        return self._remote_obligations[message_ref]

    def cached_obligations(self, logical_agent_id: str) -> list[dict]:
        return sorted(
            [
                dict(item)
                for item in self._remote_obligations.values()
                if item["logical_agent_id"] == logical_agent_id
            ],
            key=lambda item: (item["created_at"], item["message_ref"]),
        )

    async def receive_obligation_receipt(
        self,
        *,
        message_ref: str,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        attachment_node_id: str,
        seen_at: str | None = None,
        read_at: str | None = None,
        replied_at: str | None = None,
        reply_message_ref: str | None = None,
    ) -> dict:
        await self._guard_local_authority(logical_agent_id)
        attachments = await self.store.attachments_for_session(
            logical_agent_id,
            work_session_id,
            session_epoch,
            active_only=True,
        )
        if not any(item["node_instance_id"] == attachment_node_id for item in attachments):
            raise PersistentStoreError("attachment_not_active")
        receipt = await self.store.merge_message_receipt(
            message_ref,
            attachment_node_id,
            seen_at=seen_at,
            read_at=read_at,
            replied_at=replied_at,
            reply_message_ref=reply_message_ref,
        )
        return {
            "receipt": receipt,
            "gate": await self.store.fleet_gate(logical_agent_id),
            "open_obligations": await self.store.open_message_obligations(logical_agent_id),
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
        await self._guard_local_authority(logical_agent_id)
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
        await self.publish_authority(
            logical_agent_id,
            session.authority_node_id,
            session.authority_epoch,
        )
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
        peers_by_id = self.config.peers_by_id
        route = await self.route_info(logical_agent_id)
        candidates = list(self.config.peers)
        if route is not None:
            routed = peers_by_id.get(route["authority_node_id"])
            if routed is not None:
                candidates = [
                    routed,
                    *[p for p in candidates if p.instance_id != routed.instance_id],
                ]

        attempted: set[str] = set()
        wrong_authority_retry = False
        async with self.client_factory() as client:
            index = 0
            while index < len(candidates):
                peer = candidates[index]
                index += 1
                if peer.instance_id in attempted:
                    continue
                attempted.add(peer.instance_id)
                try:
                    response = await client.post(
                        f"{peer.origin}/internal/fleet/persistent/permit",
                        headers=self._headers(peer),
                        json=payload,
                    )
                    if response.status_code == 404:
                        continue
                    if response.status_code == 409 and not wrong_authority_retry:
                        body = response.json()
                        detail = body.get("detail") if isinstance(body, dict) else None
                        code = detail.get("code") if isinstance(detail, dict) else detail
                        blockers = detail.get("blockers") or [] if isinstance(detail, dict) else []
                        if code == "wrong_authority" and blockers:
                            hint = blockers[0]
                            hinted_id = str(hint.get("authority_node_id") or "")
                            hinted_epoch = int(hint.get("authority_epoch") or 0)
                            hinted_peer = peers_by_id.get(hinted_id)
                            if hinted_peer is not None and hinted_epoch > 0:
                                if self.control_store is not None:
                                    try:
                                        await self.control_store.publish_route(
                                            logical_agent_id,
                                            hinted_id,
                                            hinted_epoch,
                                        )
                                    except Exception:
                                        pass
                                if hinted_peer.instance_id not in attempted:
                                    candidates.insert(index, hinted_peer)
                                wrong_authority_retry = True
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
