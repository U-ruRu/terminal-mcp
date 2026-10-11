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
from terminal_mcp.core.public_errors import ValidationIssue, ValidationRepair, public_error
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


def _route_issue(namespace, task_id, target=None):
    """Shared local route rule for wire validation and public request repairs."""
    if namespace is None and task_id is not None:
        return ("namespace", "missing_required", "Provide namespace together with task_id.")
    if task_id is None and namespace is not None:
        return ("task_id", "missing_required", "Provide task_id together with namespace.")
    if namespace is not None and isinstance(target, str) and target.casefold() != "broadcast":
        return (
            "target",
            "conflicting_fields",
            (
                "For a task route, omit target or set it to broadcast; "
                "remove the task pair for a named target."
            ),
        )
    return None


def _route_failure(issue):
    path, reason, hint = issue
    repair = ValidationRepair(
        validation_errors=(ValidationIssue(error_class=reason, path=path, description=hint),)
    )
    return public_error(
        "input_validation_failed",
        reason=reason,
        path=path,
        details=repair,
        outcome="not_committed",
    ).as_dict()


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
        issue = _route_issue(self.namespace, self.task_id, self.target)
        if issue is not None:
            raise ValueError(issue[2])
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
    messages = {
        "cannot_message_self": "Нельзя отправить сообщение самому себе",
        "recipient_not_found": "Получатель не найден",
    }
    return {"ok": False, "code": code, "error": messages.get(code, code), "outcome": outcome}


def _public_name(slot):
    # Same stable, issuer-qualified contract as storage.access_mesh.public_name.
    digest = hashlib.sha256(f"{slot.issuer_id}:{slot.logical_agent_id}".encode()).hexdigest()[:12]
    return f"{slot.issuer_id[:40]}-{digest}"


class AccessMeshMessaging:
    def __init__(self, mesh_runtime, agent_store, replication=None):
        self.mesh = mesh_runtime
        self.agent_store = agent_store
        # Limit managed AccessMesh gates to the new local protocol only.
        # Other legacy AgentStore consumers keep their independent contracts.
        self.agent_store.message_local_prefix = f"{mesh_runtime.store.local_node_id}:localmsg:"
        # Keep coordination-obligation TTL on the same clock as message storage.
        self.agent_store.message_clock = mesh_runtime.clock
        self.replication = replication
        self.store = AccessMeshMessageStore(
            agent_store.path, mesh_runtime.store.local_node_id, now_provider=mesh_runtime.clock
        )
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

    @staticmethod
    def _session_alert_hash(node_id, slot, started_at):
        digest = hashlib.sha256(
            f"{slot.issuer_id}:{slot.slot_id}:{started_at}".encode()
        ).hexdigest()[:24]
        return f"{node_id}:localmsg:session_alert_{digest}"

    async def ensure_session_alert(self, slot, identity):
        """Atomically emit one Alert per expired cycle; test pending ACK."""
        cycle = identity.get("session_lifecycle") or {}
        started_at = cycle.get("started_at")
        expiry_text = cycle.get("hard_expires_at")
        if (
            not started_at
            or not expiry_text
            or cycle.get("state") not in {"expired", "cooldown", "draining"}
        ):
            return False
        from datetime import UTC

        try:
            expiry = datetime.fromisoformat(expiry_text.replace("Z", "+00:00"))
            if self.mesh.clock().astimezone(UTC) < expiry.astimezone(UTC):
                return False
        except (ValueError, TypeError):
            return False
        message_hash = self._session_alert_hash(self.node_id, slot, started_at)
        wire = {
            "message_hash": message_hash,
            "origin_node_id": self.node_id,
            "sender_id": "terminal-mcp-system",
            "sender_name": "Terminal MCP",
            "text": "Время сессии истекло, возвращайся в чат с промежуточным отчётом.",
            "target": identity["public_name"],
            "mode": "alert",
            "scope": "local",
            "require_reply": False,
            "alert": True,
            "created_at": expiry_text,
            "namespace": None,
            "task_id": None,
            "reply_to": None,
        }
        if await asyncio.to_thread(self.store.wire, message_hash) is None:
            await asyncio.to_thread(
                self.store.accept,
                wire,
                [
                    {
                        "logical_agent_id": identity["logical_agent_id"],
                        "public_name": identity["public_name"],
                    }
                ],
            )
        recipient = await asyncio.to_thread(
            self.store.recipient, message_hash, identity["logical_agent_id"]
        )
        return recipient["read_at"] is None

    async def _local_recipients(self):
        records = {}
        after = ""
        while True:
            slots = await asyncio.to_thread(self.mesh.store.local_slots, after=after, limit=100)
            if not slots:
                break
            for slot in slots:
                canonicalize = getattr(self.mesh.store, "canonical_slot", None)
                effective = (
                    await asyncio.to_thread(canonicalize, slot)
                    if canonicalize is not None
                    else slot
                )
                identity = await asyncio.to_thread(
                    self.mesh.store.observed_identity, effective, now=self.mesh.clock()
                )
                lifecycle = identity.get("session_lifecycle") or {}
                if (
                    not identity.get("work_session_id")
                    or identity.get("cleanup_pending")
                    or lifecycle.get("state") not in {"active", "warning", "draining"}
                ):
                    continue
                previous = records.get(identity["logical_agent_id"])
                records[identity["logical_agent_id"]] = {
                    "logical_agent_id": identity["logical_agent_id"],
                    "server_id": self.node_id,
                    "public_name": identity["public_name"],
                    "_name_aliases": list(dict.fromkeys([
                        identity["public_name"],
                        self.mesh.store.human_name(effective, short=True),
                        self.mesh.store.human_name(slot),
                        self.mesh.store.human_name(slot, short=True),
                        _public_name(effective),
                        _public_name(slot),
                        *((previous or {}).get("_name_aliases") or []),
                    ])),
                    "session_state": "active",
                    "session_started_at": lifecycle.get("started_at"),
                    "hard_expires_at": identity.get("hard_expires_at"),
                    "remaining_seconds": max(0, int(lifecycle.get("remaining_seconds") or 0)),
                    "last_active_at": await asyncio.to_thread(
                        self.store.activity, slot.issuer_id, slot.slot_id
                    ),
                }
                # More than one historical alias may have live attachments
                # to the same winner on this server. Keep the most recent
                # activity regardless of slot scan order.
                latest = records[identity["logical_agent_id"]]["last_active_at"]
                earlier = previous.get("last_active_at") if previous else None
                if earlier is not None and (latest is None or earlier > latest):
                    records[identity["logical_agent_id"]]["last_active_at"] = earlier
                if len(records) > MAX_RECIPIENTS:
                    raise MeshMessagingError("output_item_too_large")
            after = slots[-1].logical_agent_id
            if len(slots) < 100:
                break
        return list(records.values())

    async def _targets(self, wire, excluded=()):
        """Resolve recipients only from current local attachments.

        The excluded argument remains only for older internal callers; local
        delivery never carries peer-delivery exclusion metadata.
        """
        recipients = await self._local_recipients()
        if wire["namespace"] is not None:
            claims = await self.mesh.task_store.active_claims(wire["namespace"], wire["task_id"])
            owners = {item.get("owner_id") or item["agent_id"] for item in claims}
            recipients = [item for item in recipients if item["logical_agent_id"] in owners]
        if wire["target"] != "broadcast":
            target = str(wire["target"]).casefold()
            recipients = [
                item for item in recipients
                if target in {
                    str(name).casefold()
                    for name in item.get("_name_aliases", [item["public_name"]])
                }
            ]
            if len({item["logical_agent_id"] for item in recipients}) > 1:
                raise MeshMessagingError("ambiguous_agent_name")
        return [item for item in recipients if item["logical_agent_id"] != wire["sender_id"]]

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
        # Each connector reports this node's locally attached recipients only.
        # Globally replicated identities do not confer message delivery rights.
        unavailable = []
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

    async def active_agent_locations(self):
        """Return one current fleet observation per LogicalAgent.

        Agent observation remains independent from message recipient discovery.
        It uses a dedicated read-only presence endpoint so the retired message
        recipient endpoint can stay terminally disabled.
        """
        rows = {item["logical_agent_id"]: item for item in await self._local_recipients()}
        if self.replication is not None:
            for peer in self.peers:
                after = ""
                try:
                    for _ in range(1 + MAX_RECIPIENTS // 100):
                        response = await self.replication.request(
                            peer, "agents/locations", {"after": after, "limit": 100}
                        )
                        if response.get("ok") is not True or not isinstance(
                            response.get("recipients"), list
                        ):
                            break
                        for item in response["recipients"]:
                            self._validate_recipient(item, peer.instance_id)
                            known = rows.get(item["logical_agent_id"])
                            if known is None or (
                                str(item.get("last_active_at") or ""),
                                item["server_id"],
                            ) > (
                                str(known.get("last_active_at") or ""),
                                known["server_id"],
                            ):
                                rows[item["logical_agent_id"]] = item
                        following = response.get("after") or ""
                        if not following:
                            break
                        if following == after:
                            raise MeshMessagingError("message_unavailable")
                        after = following
                    if after:
                        raise MeshMessagingError("output_item_too_large")
                except Exception:
                    logging.getLogger(__name__).debug(
                        "Agent observation peer temporarily unavailable: %s",
                        peer.instance_id,
                    )
        return list(rows.values())

    def _validate_recipient(self, item, peer_id):
        if not isinstance(item, dict) or item.get("server_id") != peer_id:
            raise MeshMessagingError("message_unavailable")
        agent_id = item.get("logical_agent_id")
        if not isinstance(agent_id, str) or len(agent_id) > 160:
            raise MeshMessagingError("message_unavailable")
        slot = self.mesh.store.slot_for_agent(agent_id)
        if slot is None or item.get("public_name") != self.mesh.store.human_name(slot):
            raise MeshMessagingError("message_unavailable")

    async def handle_locations_peer(self, peer_id, payload):
        """Read-only fleet presence endpoint; it is not message recipient discovery."""
        if peer_id not in {peer.instance_id for peer in self.peers}:
            return _failure("access_denied")
        try:
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
        except ValidationError:
            return _failure("input_validation_failed")
        except MeshMessagingError as exc:
            return _failure(exc.code)

    async def _target_is_merged_self(self, target: str, logical_agent_id: str) -> bool:
        """Recognize old public_name aliases after LogicalAgent convergence."""
        # The losing issuer may exist only on another node. Its original
        # name must still be recognized from durable Mesh claims, even if
        # there has never been a local attachment for that losing slot.
        from terminal_mcp.storage.access_mesh import public_name

        number_registry = getattr(self.mesh.store, "numbers", None)
        claims = (
            await asyncio.to_thread(number_registry.snapshot) if number_registry is not None else []
        )
        for claim in claims:
            original_name = public_name(claim["issuer_id"], claim["logical_agent_id"])
            if original_name.casefold() == target.casefold():
                winner = await asyncio.to_thread(
                    self.mesh.store.numbers.group_winner,
                    claim["issuer_id"],
                    claim["slot_id"],
                )
                return winner is not None and winner["logical_agent_id"] == logical_agent_id

        cursor = ""
        while True:
            slots = await asyncio.to_thread(self.mesh.store.local_slots, after=cursor, limit=100)
            for slot in slots:
                names = {_public_name(slot).casefold()}
                try:
                    names.update({
                        self.mesh.store.human_name(slot).casefold(),
                        self.mesh.store.human_name(slot, short=True).casefold(),
                    })
                except Exception:
                    # Missing presentation metadata never creates another routing alias.
                    pass
                if target.casefold() in names:
                    canonicalize = getattr(self.mesh.store, "canonical_slot", None)
                    effective = (
                        await asyncio.to_thread(canonicalize, slot)
                        if canonicalize is not None
                        else slot
                    )
                    return effective.logical_agent_id == logical_agent_id
            if len(slots) < 100:
                return False
            cursor = slots[-1].logical_agent_id

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
            await asyncio.to_thread(self.store.recipient, reply_to, identity["logical_agent_id"])
            # An intentional second reply is another message. Each accepted
            # reply has a unique hash; inbound retry dedup uses that hash.
            # A historic name may have been reused by another slot. Replies use
            # the retained sender ID, never a current name lookup for old text.
            original = await asyncio.to_thread(
                self.mesh.store.slot_for_agent, parent["sender_id"]
            )
            if original is None:
                raise MeshMessagingError("message_unavailable")
            target, scope, namespace, task_id = _public_name(original), "local", None, None
        if isinstance(target, str) and target.casefold() == "broadcast":
            target = "broadcast"
        issue = _route_issue(namespace, task_id, target)
        if issue is not None:
            return _route_failure(issue)
        if isinstance(target, str) and (
            target.casefold() == identity["public_name"].casefold()
            or await self._target_is_merged_self(target, identity["logical_agent_id"])
        ):
            raise MeshMessagingError("cannot_message_self")
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
        # Explicit outbound sends are distinct regardless of JSON-RPC id
        # or identical text. Inbound retransmission dedup uses message_hash.
        suffix = secrets.token_hex(16)
        message_hash = f"{self.node_id}:localmsg:{suffix}"
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
        peers = []  # Node-local messages never schedule remote delivery.
        if not recipients and not peers:
            raise MeshMessagingError(
                "no_active_recipients" if wire["target"] == "broadcast" else "recipient_not_found"
            )
        if wire["target"] != "broadcast" and wire["namespace"] is None:
            # Persist the display label that was resolved for this delivery.
            # Alias input and immutable reply routing must not leak opaque names.
            wire["target"] = recipients[0]["public_name"]
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
        result = {
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

        if "delivery_recipients" in row:
            result["deliveries"] = [
                {
                    "public_name": receipt["public_name"],
                    "state": "replied"
                    if receipt["replied_at"]
                    else "read"
                    if receipt["read_at"]
                    else "seen"
                    if receipt["first_seen_at"]
                    else "delivered",
                    **{
                        key: receipt[key]
                        for key in (
                            "first_seen_at",
                            "last_seen_at",
                            "seen_count",
                            "read_at",
                            "replied_at",
                            "reply_message_hash",
                        )
                    },
                }
                for receipt in row["delivery_recipients"]
            ]
        return result

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
        incoming_hashes = {
            row["message_hash"]
            for row in raw[: len(page)]
            if row["recipient_agent_id"] == identity["logical_agent_id"]
        }
        if incoming_hashes:
            # A recipient sees an incoming message in either inbox or history.
            # Outgoing history is a read-only projection of other recipients.
            # Reserve and validate the surfaced shape before acknowledging anything.
            stamp = utc_text(self.mesh.clock())
            surfaced = [dict(item) for item in page]
            for item in surfaced:
                if item["message_hash"] not in incoming_hashes:
                    continue
                if item["mode"] == "notify":
                    item.update(
                        state="replied" if item["state"] == "replied" else "read",
                        acknowledged=True,
                    )
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
                sorted(incoming_hashes),
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
        scope="local",
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
            if scope == "fleet":
                raise MeshMessagingError("feature_disabled")
            if type(limit) is not int or not 1 <= limit <= 100:
                raise MeshMessagingError("input_validation_failed")
            if type(require_reply) is not bool or type(alert) is not bool:
                raise MeshMessagingError("input_validation_failed")
            issue = _route_issue(
                namespace, task_id, target if message_hash is None and text is not None else None
            )
            if issue is not None:
                return _route_failure(issue)
            is_read = text is None and message_hash is None
            if recipients and not is_read:
                raise MeshMessagingError("input_validation_failed")
            expiry_alert_ack = False
            if message_hash:
                wire = await asyncio.to_thread(self.store.wire, message_hash)
                expiry_alert_ack = bool(
                    wire
                    and wire.get("sender_id") == "terminal-mcp-system"
                    and wire.get("mode") == "alert"
                )
                if expiry_alert_ack and text is not None:
                    raise MeshMessagingError("message_ack_required")
            operation = (
                ManagedOperation.MESSAGE_READ
                if is_read
                else ManagedOperation.MESSAGE_REPLY
                if message_hash and text is not None
                else ManagedOperation.MESSAGE_ACK
                if message_hash
                else ManagedOperation.MESSAGE_SEND
            )
            if recipients:
                # Discovery works for anonymous callers, but a known caller is
                # excluded from its own recipient list.
                public_caller = {"logical_agent_id": "", "public_name": "anonymous"}
                try:
                    public_caller = await self.mesh.resolve(actor, ManagedOperation.MESSAGE_READ)
                except Exception:
                    pass
                return await self.recipients(public_caller, scope=scope, limit=limit, cursor=cursor)
            # Permit ACK of the current system Alert after the session ends.
            identity = await self.mesh.resolve(
                actor, ManagedOperation.MESSAGE_READ if expiry_alert_ack else operation
            )
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
        # Keep peer authentication and public size bounds, then terminally reject
        # legacy message delivery/receipt/discovery without mutating message state.
        if peer_id not in {peer.instance_id for peer in self.peers}:
            return _failure("access_denied")
        if len(canonical(payload).encode()) > MAX_WIRE_BYTES:
            return _failure("output_item_too_large")
        return _failure("feature_disabled", outcome="not_committed")

    async def flush_for_call(self, *, message_hash):
        """Compatibility no-op after retirement of peer message transport."""
        await self.flush(message_hash=message_hash)

    async def flush(self, *, message_hash=None):
        """Remote message/receipt transport is retired; no network retries."""
        return

    async def tick(self):
        # Read-time expiry is authoritative; physical pruning runs even if
        # the process was idle or restarted after a long outage.
        await asyncio.to_thread(self.store.prune_expired)

    async def start(self):
        # Idempotent transition, with its preflight inside one transaction.
        await asyncio.to_thread(self.store.activate_local_cutover)
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="access-mesh-message-retention")

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
                logging.getLogger(__name__).warning("Message retention sweep failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except TimeoutError:
                pass
