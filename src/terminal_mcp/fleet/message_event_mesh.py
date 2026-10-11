"""Issuer-bound message EVENT forwarding. This does not route chat messages."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta

import aiosqlite
from fastapi import APIRouter, Header, HTTPException

from terminal_mcp.core.orchestration import parse_utc, utc_now, utc_text

MESSAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_event_mesh_message_interest(
 issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL, recipient_agent_id TEXT NOT NULL,
 started_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 PRIMARY KEY(issuer_id,slot_id,recipient_agent_id)
);
CREATE TABLE IF NOT EXISTS mcp_event_mesh_message_versions(
 issuer_id TEXT PRIMARY KEY, version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_event_mesh_message_local_version(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 version INTEGER NOT NULL, recipients_json TEXT NOT NULL
);
"""


async def remote_message_interest(db, recipient, created_at, stamp):
    return await (
        await db.execute(
            "SELECT 1 FROM mcp_event_mesh_message_interest WHERE recipient_agent_id=? "
            "AND started_at<=? AND expires_at>? LIMIT 1",
            (recipient, created_at, stamp),
        )
    ).fetchone()


async def queue_message_events(db, stamp):
    horizon = utc_text(parse_utc(stamp) - timedelta(days=1))
    await db.execute(
        "INSERT OR IGNORE INTO mcp_event_mesh_outbox"
        "(event_id,peer_node_id,next_attempt_at,created_at) "
        "SELECT e.event_id,i.issuer_id,?,? FROM mcp_event_records e "
        "JOIN mcp_event_mesh_message_interest i ON i.recipient_agent_id=e.recipient_agent_id "
        "WHERE e.name='message.received' AND e.origin_node_id IS NULL "
        "AND i.expires_at>? AND e.occurred_at>=i.started_at AND e.occurred_at>=?",
        (stamp, stamp, stamp, horizon),
    )
    await db.execute(
        "UPDATE mcp_event_mesh_outbox SET state='dead' "
        "WHERE state IN ('pending','sending') AND event_id IN("
        "SELECT event_id FROM mcp_event_records WHERE name='message.received') "
        "AND NOT EXISTS(SELECT 1 FROM mcp_event_mesh_message_interest i "
        "JOIN mcp_event_records e ON e.recipient_agent_id=i.recipient_agent_id "
        "WHERE e.event_id=mcp_event_mesh_outbox.event_id "
        "AND i.issuer_id=mcp_event_mesh_outbox.peer_node_id)"
    )


class MessageEventMesh:
    def __init__(self, lifecycle):
        self.lifecycle = lifecycle
        self.events = lifecycle.events
        self._announced = {}
        self.peer_health = {}
        self._snapshot_lock = asyncio.Lock()

    async def snapshot(self):
        async with self._snapshot_lock:
            return await self._snapshot()

    async def _snapshot(self):
        stamp = utc_text()
        async with aiosqlite.connect(self.events.path, timeout=5) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT s.name,s.principal_id,s.credential_id,s.auth_mode,s.created_at,b.* "
                    "FROM mcp_event_subscriptions s JOIN mcp_event_message_bindings b "
                    "ON b.subscription_id=s.id WHERE s.name='message.received' "
                    "AND b.recipient_issuer_id=? AND (s.expires_at IS NULL OR s.expires_at>?)",
                    (self.events.node_id, stamp),
                )
            ).fetchall()
        recipients = {}
        for row in rows:
            if self.lifecycle.authorization_checker is not None:
                if not await self.lifecycle.authorization_checker(dict(row)):
                    continue
            key = (row["recipient_slot_id"], row["recipient_agent_id"])
            recipients[key] = min(recipients.get(key, row["created_at"]), row["created_at"])
        items = [
            {"slot_id": slot, "recipient_agent_id": agent, "created_at": recipients[(slot, agent)]}
            for slot, agent in sorted(recipients)
        ]
        encoded = json.dumps(items, separators=(",", ":"), sort_keys=True)
        async with aiosqlite.connect(self.events.path, timeout=5) as db:
            await db.execute("BEGIN IMMEDIATE")
            old = await (
                await db.execute(
                    "SELECT version,recipients_json FROM mcp_event_mesh_message_local_version "
                    "WHERE singleton=1"
                )
            ).fetchone()
            version = (old[0] + (old[1] != encoded)) if old else 1
            await db.execute(
                "INSERT INTO mcp_event_mesh_message_local_version VALUES(1,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "version=excluded.version,recipients_json=excluded.recipients_json",
                (version, encoded),
            )
            await db.commit()
        return {"version": version, "recipients": items}

    async def receive_interest(self, peer, payload):
        if (
            set(payload) != {"version", "recipients"}
            or type(payload["version"]) is not int
            or not 1 <= payload["version"] <= 2**63 - 1
            or not isinstance(payload["recipients"], list)
            or len(payload["recipients"]) > 128
        ):
            raise ValueError("invalid recipient interest")
        keys = []
        for item in payload["recipients"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"slot_id", "recipient_agent_id", "created_at"}
                or any(not isinstance(v, str) or not v or len(v) > 128 for v in item.values())
            ):
                raise ValueError("invalid recipient interest")
            if parse_utc(item["created_at"]) > utc_now() + timedelta(seconds=60):
                raise ValueError("invalid recipient interest boundary")
            keys.append((item["slot_id"], item["recipient_agent_id"]))
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate recipient interest")
        stamp = utc_text()
        expires = utc_text(utc_now() + timedelta(seconds=600))
        async with aiosqlite.connect(self.events.path, timeout=5) as db:
            await db.execute("BEGIN IMMEDIATE")
            old = await (
                await db.execute(
                    "SELECT version FROM mcp_event_mesh_message_versions WHERE issuer_id=?",
                    (peer,),
                )
            ).fetchone()
            if old and old[0] > payload["version"]:
                await db.rollback()
                return {"ok": True}
            # Peer authentication fixes the issuer. A general Fleet member cannot
            # grant access to another issuer's slot or invent a recipient identity.
            for slot, agent in keys:
                row = await (
                    await db.execute(
                        "SELECT 1 FROM access_mesh_slot_replicas WHERE issuer_id=? "
                        "AND slot_id=? AND logical_agent_id=? AND state!='deleted'",
                        (peer, slot, agent),
                    )
                ).fetchone()
                if not row:
                    raise ValueError("unknown issuer recipient")
            await db.execute(
                "INSERT INTO mcp_event_mesh_message_versions VALUES(?,?) "
                "ON CONFLICT(issuer_id) DO UPDATE SET version=excluded.version",
                (peer, payload["version"]),
            )
            existing = await (
                await db.execute(
                    "SELECT slot_id,recipient_agent_id FROM mcp_event_mesh_message_interest "
                    "WHERE issuer_id=?",
                    (peer,),
                )
            ).fetchall()
            for key in existing:
                if tuple(key) not in keys:
                    await db.execute(
                        "DELETE FROM mcp_event_mesh_message_interest WHERE issuer_id=? "
                        "AND slot_id=? AND recipient_agent_id=?",
                        (peer, *key),
                    )
            for slot, agent in keys:
                await db.execute(
                    "INSERT INTO mcp_event_mesh_message_interest VALUES(?,?,?,?,?) "
                    "ON CONFLICT(issuer_id,slot_id,recipient_agent_id) DO UPDATE SET "
                    "expires_at=excluded.expires_at",
                    (
                        peer,
                        slot,
                        agent,
                        next(
                            i["created_at"]
                            for i in payload["recipients"]
                            if i["slot_id"] == slot and i["recipient_agent_id"] == agent
                        ),
                        expires,
                    ),
                )
            more = await self.backfill(db, peer, stamp)
            await queue_message_events(db, stamp)
            await db.commit()
        return {"ok": True, "more": more}

    async def backfill(self, db, peer, stamp):
        exists = await (
            await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='access_mesh_messages'"
            )
        ).fetchone()
        if not exists:
            return False
        rows = await (
            await db.execute(
                "SELECT m.message_hash,m.text,m.created_at,m.task_namespace,w.wire_json,"
                "w.receipt_json,r.recipient_agent_id FROM coordination_messages m "
                "JOIN coordination_message_recipients r ON r.message_hash=m.message_hash "
                "JOIN access_mesh_messages w ON w.message_hash=m.message_hash "
                "JOIN mcp_event_mesh_message_interest i "
                "ON i.recipient_agent_id=r.recipient_agent_id "
                "WHERE i.issuer_id=? AND i.expires_at>? AND m.created_at>=i.started_at "
                "AND m.created_at>=? AND substr(m.message_hash,1,?)=? "
                "AND NOT EXISTS(SELECT 1 FROM mcp_event_records e WHERE "
                "e.source_key=? || m.message_hash || ':' || r.recipient_agent_id) "
                "ORDER BY m.created_at LIMIT 500",
                (
                    peer,
                    stamp,
                    utc_text(utc_now() - timedelta(days=1)),
                    len(self.events.node_id + ":localmsg:"),
                    self.events.node_id + ":localmsg:",
                    self.events.node_id + ":message.received:",
                ),
            )
        ).fetchall()
        for row in rows:
            wire = json.loads(row[4])
            deliveries = json.loads(row[5]).get("deliveries", [])
            recipient_name = next(
                (d.get("public_name") for d in deliveries if d.get("logical_agent_id") == row[6]),
                None,
            )
            if recipient_name:
                await self.events._insert_event(
                    db,
                    source_key=(self.events.node_id + ":message.received:" + row[0] + ":" + row[6]),
                    name="message.received",
                    occurred_at=row[2],
                    data={
                        "server": self.events.node_id,
                        "sender": str(wire.get("sender_name") or "")[:120],
                        "recipient": recipient_name,
                        "message_id": row[0],
                        "text": row[1],
                        "timestamp": row[2],
                    },
                    recipient_agent_id=row[6],
                    namespace=row[3],
                )

        return len(rows) >= 500

    async def receive_events(self, peer, payload):
        items = payload.get("events")
        if set(payload) != {"events"} or not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise ValueError("invalid message event batch")
        for item in items:
            if (
                not isinstance(item, dict)
                or set(item) != {"eventId", "name", "timestamp", "data", "recipient"}
                or item["name"] != "message.received"
                or not isinstance(item["eventId"], str)
                or re.fullmatch(r"evt_[0-9a-f]{32}", item["eventId"]) is None
                or not isinstance(item["timestamp"], str)
                or not isinstance(item["data"], dict)
                or not isinstance(item["recipient"], dict)
            ):
                raise ValueError("invalid message event")
            parse_utc(item["timestamp"])
            data, recipient = item["data"], item["recipient"]
            if (
                set(data) != {"server", "sender", "recipient", "message_id", "text", "timestamp"}
                or any(not isinstance(v, str) for v in data.values())
                or data["server"] != peer
                or data["timestamp"] != item["timestamp"]
                or not data["message_id"].startswith(peer + ":localmsg:")
                or set(recipient) != {"issuer_id", "slot_id", "recipient_agent_id"}
                or any(not isinstance(v, str) or not v or len(v) > 128 for v in recipient.values())
                or recipient["issuer_id"] != self.events.node_id
            ):
                raise ValueError("invalid message event recipient")
        stamp, horizon = utc_text(), utc_text(utc_now() - timedelta(days=1))
        async with aiosqlite.connect(self.events.path, timeout=5) as db:
            await db.execute("BEGIN IMMEDIATE")
            for item in items:
                if item["timestamp"] < horizon:
                    continue
                r = item["recipient"]
                active = await (
                    await db.execute(
                        "SELECT 1 FROM mcp_event_subscriptions s JOIN mcp_event_message_bindings b "
                        "ON b.subscription_id=s.id WHERE s.name='message.received' "
                        "AND b.recipient_agent_id=? AND b.recipient_slot_id=? "
                        "AND b.recipient_issuer_id=? AND b.recipient_node_id=? "
                        "AND s.created_at<=? AND (s.expires_at IS NULL OR s.expires_at>?) "
                        "AND (s.expires_at IS NULL OR s.expires_at>?) LIMIT 1",
                        (
                            r["recipient_agent_id"],
                            r["slot_id"],
                            r["issuer_id"],
                            self.events.node_id,
                            item["timestamp"],
                            item["timestamp"],
                            stamp,
                        ),
                    )
                ).fetchone()
                if active:
                    await db.execute(
                        "INSERT OR IGNORE INTO mcp_event_records"
                        "(event_id,source_key,name,occurred_at,data_json,recipient_agent_id,"
                        "origin_node_id) VALUES(?,?,?,?,?,?,?)",
                        (
                            item["eventId"],
                            "mesh:" + peer + ":" + item["eventId"],
                            item["name"],
                            item["timestamp"],
                            json.dumps(item["data"], ensure_ascii=False),
                            r["recipient_agent_id"],
                            peer,
                        ),
                    )
            await db.commit()
        return {"ok": True, "acked": [item["eventId"] for item in items]}

    async def due(self, peer):
        stamp = utc_text()
        async with aiosqlite.connect(self.events.path, timeout=5) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await queue_message_events(db, stamp)
            await db.execute(
                "UPDATE mcp_event_mesh_outbox SET state=CASE WHEN attempts>=288 "
                "THEN 'dead' ELSE 'pending' END WHERE state='sending' "
                "AND next_attempt_at<=?",
                (stamp,),
            )
            rows = await (
                await db.execute(
                    "SELECT o.event_id,e.occurred_at,e.data_json,i.slot_id,i.recipient_agent_id "
                    "FROM mcp_event_mesh_outbox o "
                    "JOIN mcp_event_records e ON e.event_id=o.event_id "
                    "JOIN mcp_event_mesh_message_interest i ON i.issuer_id=o.peer_node_id "
                    "AND i.recipient_agent_id=e.recipient_agent_id "
                    "WHERE o.peer_node_id=? AND o.state='pending' AND o.next_attempt_at<=? "
                    "AND o.attempts<288 AND i.expires_at>? AND e.name='message.received' "
                    "ORDER BY o.next_attempt_at LIMIT 20",
                    (peer, stamp, stamp),
                )
            ).fetchall()
            items = []
            for row in rows:
                item = {
                    "eventId": row["event_id"],
                    "name": "message.received",
                    "timestamp": row["occurred_at"],
                    "data": json.loads(row["data_json"]),
                    "recipient": {
                        "issuer_id": peer,
                        "slot_id": row["slot_id"],
                        "recipient_agent_id": row["recipient_agent_id"],
                    },
                }
                size = len(json.dumps({"events": items + [item]}, ensure_ascii=False).encode())
                if size > 64 * 1024:
                    if not items:
                        await db.execute(
                            "UPDATE mcp_event_mesh_outbox SET state='dead' "
                            "WHERE event_id=? AND peer_node_id=?",
                            (row["event_id"], peer),
                        )
                        continue
                    break
                items.append(item)
                await db.execute(
                    "UPDATE mcp_event_mesh_outbox SET state='sending',attempts=attempts+1, "
                    "next_attempt_at=? WHERE event_id=? AND peer_node_id=?",
                    (utc_text(utc_now() + timedelta(seconds=120)), row["event_id"], peer),
                )
            await db.commit()
        return items

    async def sync_peer(self, peer):
        pending = []
        try:
            snapshot = await self.snapshot()
            current = asyncio.get_running_loop().time()
            old = self._announced.get(peer.instance_id)
            if old is None or old[0] != snapshot["version"] or old[1] <= current:
                result = await self.lifecycle.request(peer, "message-events/interest", snapshot)
                if result.get("ok") is not True:
                    raise ValueError("message interest rejected")
                self._announced[peer.instance_id] = (
                    snapshot["version"],
                    0.0 if result.get("more") else current + 120,
                )
            pending = await self.due(peer.instance_id)
            if pending:
                result = await self.lifecycle.request(
                    peer, "message-events/events", {"events": pending}
                )
                ids = {item["eventId"] for item in pending}
                acked = result.get("acked", [])
                if (
                    result.get("ok") is not True
                    or not isinstance(acked, list)
                    or any(not isinstance(v, str) for v in acked)
                    or not set(acked) <= ids
                ):
                    raise ValueError("invalid message event acknowledgement")
                await self.lifecycle.finish(peer.instance_id, ids, set(acked))
            self.peer_health[peer.instance_id] = {"status": "healthy"}
        except Exception as exc:
            if pending:
                try:
                    await self.lifecycle.finish(
                        peer.instance_id, [i["eventId"] for i in pending], set()
                    )
                except Exception:
                    pass
            self._announced.pop(peer.instance_id, None)
            self.peer_health[peer.instance_id] = {
                "status": "degraded",
                "reason": type(exc).__name__,
            }


def build_message_event_router(messages, auth, trusted):
    router = APIRouter()

    def authenticate(peer_id, authorization, payload):
        peer = auth.authenticate(peer_id, authorization)
        if peer is None or peer.instance_id not in trusted:
            raise HTTPException(401, "invalid access mesh peer")
        if len(json.dumps(payload, separators=(",", ":")).encode()) > 64 * 1024:
            raise HTTPException(413, "message event payload exceeds byte budget")
        return peer.instance_id

    @router.post("/internal/fleet/access-mesh/message-events/interest", include_in_schema=False)
    async def interest(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization, payload)
        try:
            return await messages.receive_interest(peer, payload)
        except (ValueError, TypeError):
            raise HTTPException(400, "invalid message event interest") from None

    @router.post("/internal/fleet/access-mesh/message-events/events", include_in_schema=False)
    async def events(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization, payload)
        try:
            return await messages.receive_events(peer, payload)
        except (ValueError, TypeError):
            raise HTTPException(400, "invalid message events") from None

    return router
