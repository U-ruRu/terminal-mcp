"""Local-first messaging for independently admitted Access Mesh connectors.

Only the local runtime admits a caller. Fleet requests carry durable delivery or
receipt data; they are never required to read, acknowledge or release a local
obligation. Every accepted forwarding job survives restart and a lost HTTP ACK.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.core.read_contract import (
    InvalidCursor,
    OutputItemTooLarge,
    bounded_page,
    decode_cursor,
    encode_cursor,
    summary_message,
)
from terminal_mcp.storage.access_mesh_messages import (
    AccessMeshMessageStore,
    MeshMessagingError,
    canonical,
)

MAX_WIRE_BYTES = 64 * 1024
PUBLIC_DELIVERY_BUDGET_SECONDS = 1.0
MAX_RECIPIENTS = 256
RECIPIENT_FIELDS = (
    "public_name",
    "session_state",
    "session_started_at",
    "hard_expires_at",
    "remaining_seconds",
    "last_active_at",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MeshWireMessage(_Strict):
    message_hash: str = Field(min_length=1, max_length=128)
    origin_node_id: str = Field(min_length=1, max_length=80)
    sender_id: str = Field(min_length=1, max_length=160)
    sender_name: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=16384)
    target: str = Field(min_length=1, max_length=120)
    scope: Literal["local", "fleet"]
    mode: Literal["notify", "ack", "alert"]
    require_reply: bool
    alert: bool
    created_at: str = Field(min_length=1, max_length=40)
    namespace: str | None = Field(default=None, min_length=1, max_length=120)
    task_id: str | None = Field(default=None, min_length=1, max_length=120)
    reply_to: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def valid_route(self):
        if (self.namespace is None) != (self.task_id is None):
            raise ValueError("namespace and task_id form one task target")
        if self.namespace is not None and self.target != "broadcast":
            raise ValueError("choose public_name or a task target")
        stamp = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("created_at requires an explicit timezone")
        return self


class MeshDelivery(_Strict):
    message: MeshWireMessage
    excluded: list[str] = Field(max_length=MAX_RECIPIENTS + 1)


class MeshReceipt(_Strict):
    message_hash: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=160)
    read_at: str | None = Field(default=None, max_length=40)
    replied_at: str | None = Field(default=None, max_length=40)
    reply_message_hash: str | None = Field(default=None, max_length=128)


class MeshRecipientsQuery(_Strict):
    after: str = Field(default="", max_length=160)
    limit: int = Field(default=100, ge=1, le=100)


def _failure(code, *, outcome="not_committed"):
    return {"ok": False, "code": code, "error": code, "outcome": outcome}


def _public_name(slot):
    # Same stable, issuer-qualified contract as storage.access_mesh.public_name.
    digest = hashlib.sha256(f"{slot.issuer_id}:{slot.logical_agent_id}".encode()).hexdigest()[:12]
    return f"{slot.issuer_id[:40]}-{digest}"


class AccessMeshMessaging:
    def __init__(self, mesh_runtime, agent_store, replication=None):
        self.mesh = mesh_runtime
        self.agent_store = agent_store
        self.replication = replication
        self.store = AccessMeshMessageStore(agent_store.path, mesh_runtime.store.local_node_id)
        self._flush_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task = None
        self.last_error = None

    @property
    def peers(self):
        return tuple(self.replication.peers) if self.replication is not None else ()

    @property
    def node_id(self):
        return self.mesh.store.local_node_id

    async def _local_recipients(self):
        records = {}
        after = ""
        while True:
            slots = await asyncio.to_thread(self.mesh.store.local_slots, after=after, limit=100)
            if not slots:
                break
            for slot in slots:
                identity = await asyncio.to_thread(
                    self.mesh.store.observed_identity, slot, now=self.mesh.clock()
                )
                lifecycle = identity.get("session_lifecycle") or {}
                if (
                    not identity.get("work_session_id")
                    or identity.get("cleanup_pending")
                    or lifecycle.get("state") not in {"active", "warning", "draining"}
                ):
                    continue
                records[slot.logical_agent_id] = {
                    "logical_agent_id": slot.logical_agent_id,
                    "server_id": self.node_id,
                    "public_name": identity["public_name"],
                    "session_state": "active",
                    "session_started_at": lifecycle.get("started_at"),
                    "hard_expires_at": identity.get("hard_expires_at"),
                    "remaining_seconds": max(0, int(lifecycle.get("remaining_seconds") or 0)),
                    "last_active_at": await asyncio.to_thread(
                        self.store.activity, slot.issuer_id, slot.slot_id
                    ),
                }
                if len(records) > MAX_RECIPIENTS:
                    raise MeshMessagingError("output_item_too_large")
            after = slots[-1].logical_agent_id
            if len(slots) < 100:
                break
        return list(records.values())

    async def _targets(self, wire, excluded):
        # A reply returns to the parent message's originating execution node.
        # A second attachment of its sender must not consume that reply locally
        # and exclude the node where the sender is reading the original thread.
        if wire.get("reply_to") and wire["reply_to"].split(":meshmsg:", 1)[0] != self.node_id:
            return []
        recipients = await self._local_recipients()
        if wire["namespace"] is not None:
            claims = await self.mesh.task_store.active_claims(wire["namespace"], wire["task_id"])
            owners = {item.get("owner_id") or item["agent_id"] for item in claims}
            recipients = [item for item in recipients if item["logical_agent_id"] in owners]
        if wire["target"] != "broadcast":
            recipients = [item for item in recipients if item["public_name"] == wire["target"]]
        # A directed message must be available in the target agent's inbox
        # on each node where that agent has an active local attachment.
        # Excluded agents represent already-delivered *broadcast* recipients;
        # applying that list to a directed message silently drops a legitimate
        # receiver when the same LogicalAgent is attached on both nodes.
        # A recipient is still deduplicated transactionally by message_hash
        # within each node, and a sender never receives its own message.
        excluded_ids = {wire["sender_id"]}
        if wire["target"] == "broadcast":
            excluded_ids.update(excluded)
        return [item for item in recipients if item["logical_agent_id"] not in excluded_ids]

    async def recipients(self, identity, *, scope, limit, cursor):
        query = {
            "kind": "mesh.message.recipients",
            "node": self.node_id,
            "agent": identity["logical_agent_id"],
            "scope": scope,
        }
        # Validate before doing any peer work.
        decode_cursor(cursor, query)
        rows = {item["logical_agent_id"]: item for item in await self._local_recipients()}
        unavailable = []
        if scope == "fleet":
            for peer in self.peers:
                after = ""
                try:
                    for _ in range(1 + MAX_RECIPIENTS // 100):
                        result = await self.replication.request(
                            peer, "messages/recipients", {"after": after, "limit": 100}
                        )
                        if result.get("ok") is not True or not isinstance(
                            result.get("recipients"), list
                        ):
                            raise MeshMessagingError("message_unavailable")
                        for item in result["recipients"]:
                            self._validate_recipient(item, peer.instance_id)
                            rows.setdefault(item["logical_agent_id"], item)
                        after = result.get("after")
                        if not after:
                            break
                    if after:
                        raise MeshMessagingError("output_item_too_large")
                except Exception:
                    unavailable.append(peer.instance_id)
        rows.pop(identity["logical_agent_id"], None)
        if len(rows) > MAX_RECIPIENTS:
            raise MeshMessagingError("output_item_too_large")
        public = [
            {key: item.get(key) for key in RECIPIENT_FIELDS}
            for item in sorted(rows.values(), key=lambda row: row["public_name"])
        ]
        page, next_cursor = bounded_page(public, limit=limit, cursor=cursor, scope=query)
        return {
            "ok": True,
            "action": "recipients",
            "sender": identity["public_name"],
            "recipients": page,
            "next_cursor": next_cursor,
            "unavailable_peers": unavailable,
        }

    def _validate_recipient(self, item, peer_id):
        if not isinstance(item, dict) or item.get("server_id") != peer_id:
            raise MeshMessagingError("message_unavailable")
        agent_id = item.get("logical_agent_id")
        if not isinstance(agent_id, str) or len(agent_id) > 160:
            raise MeshMessagingError("message_unavailable")
        slot = self.mesh.store.slot_for_agent(agent_id)
        if slot is None or item.get("public_name") != _public_name(slot):
            raise MeshMessagingError("message_unavailable")

    async def _send(
        self,
        actor,
        identity,
        *,
        text,
        target,
        mode,
        scope,
        require_reply,
        alert,
        namespace,
        task_id,
        reply_to=None,
    ):
        if reply_to:
            parent = await asyncio.to_thread(self.store.wire, reply_to)
            if parent is None:
                raise MeshMessagingError("message_not_found")
            row = await asyncio.to_thread(
                self.store.recipient, reply_to, identity["logical_agent_id"]
            )
            if row["reply_message_hash"]:
                previous = await asyncio.to_thread(self.store.wire, row["reply_message_hash"])
                if previous is None or previous["text"] != text:
                    raise MeshMessagingError("idempotency_conflict")
                return await asyncio.to_thread(self.store.receipt, row["reply_message_hash"])
            target, scope, namespace, task_id = parent["sender_name"], "fleet", None, None
        if isinstance(target, str) and target.casefold() == "broadcast":
            target = "broadcast"
        payload = {
            "sender_id": identity["logical_agent_id"],
            "sender_name": identity["public_name"],
            "text": text,
            "target": target or "broadcast",
            "mode": mode,
            "scope": scope,
            "require_reply": require_reply,
            "alert": alert,
            "namespace": namespace,
            "task_id": task_id,
            "reply_to": reply_to,
        }
        key = {
            "connection": self.mesh.connection_key(actor),
            "cycle": identity["work_session_id"],
            "epoch": identity["session_epoch"],
            "request": actor.request_id,
            "payload": payload,
        }
        suffix = (
            hashlib.sha256(canonical(key).encode()).hexdigest()[:40]
            if actor.request_id is not None
            else secrets.token_hex(20)
        )
        message_hash = f"{self.node_id}:meshmsg:{suffix}"
        existing = await asyncio.to_thread(self.store.wire, message_hash)
        if existing is not None:
            await self.flush_for_call(message_hash=message_hash)
            return await asyncio.to_thread(self.store.receipt, message_hash)
        wire = MeshWireMessage.model_validate(
            {
                "message_hash": message_hash,
                "origin_node_id": self.node_id,
                "created_at": utc_text(self.mesh.clock()),
                **payload,
            }
        ).model_dump()
        if len(canonical(wire).encode()) > MAX_WIRE_BYTES - 8192:
            raise MeshMessagingError("output_item_too_large")
        recipients = await self._targets(wire, ())
        peers = [peer.instance_id for peer in self.peers] if scope == "fleet" else []
        if not recipients and not peers:
            raise MeshMessagingError(
                "no_active_recipients" if wire["target"] == "broadcast" else "recipient_not_found"
            )
        # The accepted transaction is the commit point. Network or readback errors
        # afterwards can only change delivery status, never the mutation outcome.
        receipt = await asyncio.to_thread(
            self.store.accept,
            wire,
            recipients,
            peers=peers,
            reply_author=identity["logical_agent_id"] if reply_to else None,
        )
        try:
            if peers:
                await self.flush_for_call(message_hash=message_hash)
            receipt = await asyncio.to_thread(self.store.receipt, message_hash)
        except Exception:
            logging.getLogger(__name__).warning(
                "Post-commit message delivery projection failed", exc_info=True
            )
        if receipt["state"] == "no_recipients":
            return {
                **receipt,
                **_failure(
                    "no_active_recipients"
                    if wire["target"] == "broadcast"
                    else "recipient_not_found",
                    outcome="committed",
                ),
            }
        return receipt

    @staticmethod
    def _view(row):
        wire = json.loads(row["wire_json"])
        state = (
            "replied"
            if row["replied_at"]
            else "read"
            if row["read_at"]
            else ("seen" if row["first_seen_at"] else "delivered")
        )
        return {
            "message_hash": row["message_hash"],
            "sender": wire["sender_name"],
            "target": row["target_name"],
            "text": row["text"],
            "mode": wire["mode"],
            "scope": wire["scope"],
            "state": state,
            "created_at": row["created_at"],
            "namespace": row["task_namespace"],
            "task_id": row["task_id"],
            "read_at": row["read_at"],
            "replied_at": row["replied_at"],
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "seen_count": row["seen_count"] or 0,
            "reply_message_hash": row["reply_message_hash"],
            "reply_to": wire["reply_to"],
        }

    async def read(
        self,
        identity,
        *,
        history,
        limit,
        cursor,
        detail,
        namespace,
        task_id,
        response_preflight=None,
    ):
        query = {
            "kind": "mesh.message.history" if history else "mesh.message.inbox",
            "node": self.node_id,
            "agent": identity["logical_agent_id"],
            "detail": detail,
            "namespace": namespace,
            "task_id": task_id,
        }
        before = decode_cursor(cursor, query)
        raw = await asyncio.to_thread(
            self.store.page,
            identity["logical_agent_id"],
            history=history,
            before=before,
            limit=limit + 1,
            namespace=namespace,
            task_id=task_id,
        )
        candidate = [self._view(row) for row in raw[:limit]]
        if detail == "summary":
            candidate = [
                {"message_hash": row["message_hash"], **summary_message(row)} for row in candidate
            ]
        page, _ = bounded_page(candidate, limit=limit, cursor=None, scope={"kind": "message-page"})
        next_cursor = (
            encode_cursor(raw[len(page) - 1]["sequence"], query)
            if page and len(raw) > len(page)
            else None
        )
        result = {
            "ok": True,
            "action": "history" if history else "inbox",
            "sender": identity["public_name"],
            "messages": page,
            "next_cursor": next_cursor,
        }
        if not history and page:
            # Reserve and validate the surfaced shape before acknowledging anything.
            stamp = utc_text(self.mesh.clock())
            surfaced = [dict(item) for item in page]
            for item in surfaced:
                if item["mode"] == "notify":
                    item.update(state="read", acknowledged=True)
                    if detail == "full":
                        item["read_at"] = item.get("read_at") or stamp
                elif item["state"] == "delivered":
                    item["state"] = "seen"
                if detail == "full":
                    item.update(
                        first_seen_at=item.get("first_seen_at") or stamp,
                        last_seen_at=stamp,
                        seen_count=item["seen_count"] + 1,
                    )
            projected = {**result, "messages": surfaced}
            if response_preflight:
                failure = response_preflight(projected)
                if failure:
                    return failure
            await asyncio.to_thread(
                self.store.surface,
                identity["logical_agent_id"],
                [row["message_hash"] for row in raw[: len(page)]],
                stamp,
            )
            result = projected
        return result

    async def message(
        self,
        actor,
        *,
        text=None,
        target=None,
        message_hash=None,
        mode=None,
        require_reply=False,
        alert=False,
        scope="fleet",
        history=False,
        recipients=False,
        limit=20,
        cursor=None,
        detail="summary",
        namespace=None,
        task_id=None,
        response_preflight=None,
    ):
        try:
            if scope not in {"local", "fleet"} or detail not in {"summary", "full"}:
                raise MeshMessagingError("input_validation_failed")
            if type(limit) is not int or not 1 <= limit <= 100:
                raise MeshMessagingError("input_validation_failed")
            if type(require_reply) is not bool or type(alert) is not bool:
                raise MeshMessagingError("input_validation_failed")
            if (namespace is None) != (task_id is None):
                raise MeshMessagingError("invalid_task_target")
            is_read = text is None and message_hash is None
            if recipients and not is_read:
                raise MeshMessagingError("input_validation_failed")
            operation = (
                ManagedOperation.MESSAGE_READ
                if is_read
                else ManagedOperation.MESSAGE_REPLY
                if message_hash and text is not None
                else ManagedOperation.MESSAGE_ACK
                if message_hash
                else ManagedOperation.MESSAGE_SEND
            )
            identity = await self.mesh.resolve(actor, operation)
            if recipients:
                return await self.recipients(identity, scope=scope, limit=limit, cursor=cursor)
            if is_read:
                return await self.read(
                    identity,
                    history=history,
                    limit=limit,
                    cursor=cursor,
                    detail=detail,
                    namespace=namespace,
                    task_id=task_id,
                    response_preflight=response_preflight,
                )
            if message_hash and text is None:
                return await asyncio.to_thread(
                    self.store.acknowledge,
                    message_hash,
                    identity["logical_agent_id"],
                    utc_text(self.mesh.clock()),
                )
            normalized_mode = mode or ("alert" if alert or require_reply else "notify")
            if normalized_mode not in {"notify", "ack", "alert"}:
                raise MeshMessagingError("invalid_message_mode")
            if require_reply or alert:
                normalized_mode = "alert"
            return await self._send(
                actor,
                identity,
                text=text,
                target=target,
                mode=normalized_mode,
                scope=scope,
                require_reply=require_reply or normalized_mode == "alert",
                alert=alert or normalized_mode == "alert",
                namespace=namespace,
                task_id=task_id,
                reply_to=message_hash,
            )
        except InvalidCursor:
            return _failure("invalid_cursor")
        except OutputItemTooLarge:
            return _failure("output_item_too_large")
        except ValidationError:
            return _failure("input_validation_failed")
        except MeshMessagingError as exc:
            return _failure(exc.code)
        except Exception as exc:
            if isinstance(getattr(exc, "code", None), str):
                return _failure(exc.code)
            raise

    async def handle_peer(self, peer_id, kind, payload):
        if peer_id not in {peer.instance_id for peer in self.peers}:
            return _failure("access_denied")
        if len(canonical(payload).encode()) > MAX_WIRE_BYTES:
            return _failure("output_item_too_large")
        try:
            if kind == "deliver":
                request = MeshDelivery.model_validate(payload)
                wire = request.message.model_dump()
                if (
                    wire["origin_node_id"] != peer_id
                    or wire["scope"] != "fleet"
                    or not wire["message_hash"].startswith(f"{peer_id}:meshmsg:")
                ):
                    raise MeshMessagingError("message_forbidden")
                slot = await asyncio.to_thread(self.mesh.store.slot_for_agent, wire["sender_id"])
                if slot is None or _public_name(slot) != wire["sender_name"]:
                    raise MeshMessagingError("sender_not_authorized")
                recipients = await self._targets(wire, request.excluded)
                return await asyncio.to_thread(self.store.accept, wire, recipients)
            if kind == "receipt":
                receipt = MeshReceipt.model_validate(payload).model_dump()
                return await asyncio.to_thread(self.store.apply_receipt, peer_id, receipt)
            if kind == "recipients":
                request = MeshRecipientsQuery.model_validate(payload)
                rows = [
                    item
                    for item in await self._local_recipients()
                    if item["logical_agent_id"] > request.after
                ]
                rows.sort(key=lambda item: item["logical_agent_id"])
                page = rows[: request.limit]
                return {
                    "ok": True,
                    "recipients": page,
                    "after": page[-1]["logical_agent_id"] if len(rows) > len(page) else None,
                }
            return _failure("input_validation_failed")
        except ValidationError:
            return _failure("input_validation_failed")
        except MeshMessagingError as exc:
            return _failure(exc.code)

    async def flush_for_call(self, *, message_hash):
        """The durable accept receipt does not wait for a busy or offline Fleet."""
        try:
            await asyncio.wait_for(
                self.flush(message_hash=message_hash),
                timeout=PUBLIC_DELIVERY_BUDGET_SECONDS,
            )
        except TimeoutError:
            # The transaction has already queued every remaining delivery.
            # Lifespan workers retry it without extending this caller's budget.
            return

    async def flush(self, *, message_hash=None):
        if self.replication is None:
            return
        async with self._flush_lock:
            peers = {peer.instance_id: peer for peer in self.peers}
            jobs = await asyncio.to_thread(self.store.pending, limit=20, message_hash=message_hash)
            for job in jobs:
                try:
                    peer = peers.get(job["peer_id"])
                    if peer is None:
                        raise MeshMessagingError("message_unavailable")
                    result = await self.replication.request(
                        peer, f"messages/{job['kind']}", json.loads(job["payload_json"])
                    )
                    if (
                        result.get("ok") is not True
                        or result.get("message_hash") != job["message_hash"]
                    ):
                        raise MeshMessagingError("message_unavailable")
                    if job["kind"] == "deliver":
                        deliveries = result.get("deliveries")
                        if not isinstance(deliveries, list) or len(deliveries) > MAX_RECIPIENTS:
                            raise MeshMessagingError("message_unavailable")
                        for item in deliveries:
                            self._validate_recipient(item, peer.instance_id)
                    await asyncio.to_thread(
                        self.store.finish_delivery, job, result, utc_text(self.mesh.clock())
                    )
                except Exception as exc:
                    await asyncio.to_thread(
                        self.store.failed_delivery,
                        job["id"],
                        str(getattr(exc, "code", "message_unavailable")),
                    )

    async def tick(self):
        await self.flush()

    async def start(self):
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="access-mesh-message-outbox")

    async def stop(self):
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self):
        while not self._stop.is_set():
            try:
                await self.tick()
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__
                logging.getLogger(__name__).warning("Message outbox sweep failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2)
            except TimeoutError:
                pass
