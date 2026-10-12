"""Issuer-bound message EVENT forwarding. This does not route chat messages."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from datetime import timedelta

import aiosqlite
from fastapi import APIRouter, Header, HTTPException

from terminal_mcp.core.orchestration import parse_utc, utc_now, utc_text
from terminal_mcp.fleet.access_mesh import authenticate_access_mesh_peer

PRIVATE_EVENT_BYTES = 128 * 1024
CHUNK_BYTES = 32 * 1024
RECIPIENT_PAGE = 128


def wire_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


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

CREATE TABLE IF NOT EXISTS mcp_event_mesh_message_interest_progress(
 issuer_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
 next_offset INTEGER NOT NULL, complete INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_event_mesh_message_interest_parts(
 issuer_id TEXT NOT NULL, version INTEGER NOT NULL, ordinal INTEGER NOT NULL,
 slot_id TEXT NOT NULL, recipient_agent_id TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(issuer_id,version,ordinal),
 UNIQUE(issuer_id,version,slot_id,recipient_agent_id)
);
CREATE TABLE IF NOT EXISTS mcp_event_mesh_message_chunks(
 source_node_id TEXT NOT NULL, event_id TEXT NOT NULL, part_index INTEGER NOT NULL,
 metadata_json TEXT NOT NULL, data BLOB NOT NULL, stored_at TEXT NOT NULL,
 PRIMARY KEY(source_node_id,event_id,part_index)
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
        self._interest_progress = {}

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
            set(payload) != {"version", "recipients", "offset", "complete"}
            or type(payload["version"]) is not int
            or not 1 <= payload["version"] <= 2**63 - 1
            or type(payload["offset"]) is not int
            or payload["offset"] < 0
            or type(payload["complete"]) is not bool
            or not isinstance(payload["recipients"], list)
            or len(payload["recipients"]) > RECIPIENT_PAGE
            or (not payload["complete"] and len(payload["recipients"]) != RECIPIENT_PAGE)
        ):
            raise ValueError("invalid recipient interest page")
        keys = []
        for item in payload["recipients"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"slot_id", "recipient_agent_id", "created_at"}
                or any(not isinstance(v, str) or not v or len(v) > 128 for v in item.values())
                or len(item["created_at"]) > 40
                or parse_utc(item["created_at"]) > utc_now() + timedelta(seconds=60)
            ):
                raise ValueError("invalid recipient interest")
            keys.append((item["slot_id"], item["recipient_agent_id"]))
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate recipient interest")
        version, offset = payload["version"], payload["offset"]
        stamp, expires = utc_text(), utc_text(utc_now() + timedelta(seconds=600))
        async with aiosqlite.connect(self.events.path, timeout=5) as db:
            await db.execute("BEGIN IMMEDIATE")
            progress = await (
                await db.execute(
                    "SELECT version,next_offset,complete "
                    "FROM mcp_event_mesh_message_interest_progress "
                    "WHERE issuer_id=?",
                    (peer,),
                )
            ).fetchone()
            if progress and progress[0] > version:
                await db.rollback()
                return {"ok": True, "next_offset": progress[1], "complete": bool(progress[2])}
            if progress and progress[0] == version and offset > 0 and offset < progress[1]:
                await db.rollback()
                return {"ok": True, "next_offset": progress[1], "complete": bool(progress[2])}
            if offset == 0:
                await db.execute(
                    "DELETE FROM mcp_event_mesh_message_interest_parts WHERE issuer_id=?", (peer,)
                )
                progress = (version, 0, False)
            elif not progress or progress[0] != version or progress[1] != offset:
                raise ValueError("out of order recipient interest page")
            for item in payload["recipients"]:
                known = await (
                    await db.execute(
                        "SELECT 1 FROM access_mesh_slot_replicas WHERE issuer_id=? "
                        "AND slot_id=? AND logical_agent_id=? AND state!='deleted'",
                        (peer, item["slot_id"], item["recipient_agent_id"]),
                    )
                ).fetchone()
                if not known:
                    raise ValueError("unknown issuer recipient")
            for index, item in enumerate(payload["recipients"], offset):
                await db.execute(
                    "INSERT INTO mcp_event_mesh_message_interest_parts VALUES(?,?,?,?,?,?)",
                    (
                        peer,
                        version,
                        index,
                        item["slot_id"],
                        item["recipient_agent_id"],
                        item["created_at"],
                    ),
                )
            next_offset = offset + len(keys)
            complete = payload["complete"]
            await db.execute(
                "INSERT INTO mcp_event_mesh_message_interest_progress VALUES(?,?,?,?) "
                "ON CONFLICT(issuer_id) DO UPDATE SET version=excluded.version,"
                "next_offset=excluded.next_offset,complete=excluded.complete",
                (peer, version, next_offset, int(complete)),
            )
            if complete:
                # Only a complete authenticated version may withdraw old interests.
                await db.execute(
                    "DELETE FROM mcp_event_mesh_message_interest WHERE issuer_id=? AND NOT EXISTS("
                    "SELECT 1 FROM mcp_event_mesh_message_interest_parts p "
                    "WHERE p.issuer_id=? AND p.version=? "
                    "AND p.slot_id=mcp_event_mesh_message_interest.slot_id "
                    "AND p.recipient_agent_id=mcp_event_mesh_message_interest.recipient_agent_id)",
                    (peer, peer, version),
                )
                await db.execute(
                    "INSERT INTO mcp_event_mesh_message_interest "
                    "SELECT issuer_id,slot_id,recipient_agent_id,created_at,? "
                    "FROM mcp_event_mesh_message_interest_parts WHERE issuer_id=? AND version=? "
                    "ON CONFLICT(issuer_id,slot_id,recipient_agent_id) DO UPDATE SET "
                    "expires_at=excluded.expires_at",
                    (expires, peer, version),
                )
                await db.execute(
                    "INSERT INTO mcp_event_mesh_message_versions VALUES(?,?) "
                    "ON CONFLICT(issuer_id) DO UPDATE SET version=excluded.version",
                    (peer, version),
                )
                await self.backfill(db, peer, stamp)
                await queue_message_events(db, stamp)
            await db.commit()
        return {"ok": True, "next_offset": next_offset, "complete": complete}

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

    async def receive_chunk(self, peer, payload):
        if (
            set(payload)
            != {"eventId", "timestamp", "recipient", "digest", "index", "total", "part"}
            or not isinstance(payload["eventId"], str)
            or re.fullmatch(r"evt_[0-9a-f]{32}", payload["eventId"]) is None
            or not isinstance(payload["digest"], str)
            or re.fullmatch(r"[0-9a-f]{64}", payload["digest"]) is None
            or type(payload["index"]) is not int
            or type(payload["total"]) is not int
            or not 1 <= payload["total"] <= PRIVATE_EVENT_BYTES // CHUNK_BYTES
            or not 0 <= payload["index"] < payload["total"]
            or not isinstance(payload["timestamp"], str)
            or len(payload["timestamp"]) > 40
            or not isinstance(payload["recipient"], dict)
            or set(payload["recipient"]) != {"issuer_id", "slot_id", "recipient_agent_id"}
            or payload["recipient"]["issuer_id"] != self.events.node_id
            or any(
                not isinstance(v, str) or not v or len(v) > 128
                for v in payload["recipient"].values()
            )
            or not isinstance(payload["part"], str)
        ):
            raise ValueError("invalid message chunk")
        parse_utc(payload["timestamp"])
        part = base64.b64decode(payload["part"], validate=True)
        if len(part) > CHUNK_BYTES:
            raise ValueError("message chunk exceeds byte budget")
        recipient = payload["recipient"]
        metadata = {k: v for k, v in payload.items() if k not in ("index", "part")}
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
        stamp, horizon = utc_text(), utc_text(utc_now() - timedelta(days=1))
        assembled = None
        async with aiosqlite.connect(self.events.path, timeout=5) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "DELETE FROM mcp_event_mesh_message_chunks WHERE stored_at<?", (horizon,)
            )
            active = await (
                await db.execute(
                    "SELECT 1 FROM mcp_event_subscriptions s JOIN mcp_event_message_bindings b "
                    "ON b.subscription_id=s.id WHERE s.name='message.received' "
                    "AND b.recipient_agent_id=? AND b.recipient_slot_id=? "
                    "AND b.recipient_issuer_id=? AND b.recipient_node_id=? AND s.created_at<=? "
                    "AND (s.expires_at IS NULL OR s.expires_at>?) "
                    "AND (s.expires_at IS NULL OR s.expires_at>?) LIMIT 1",
                    (
                        recipient["recipient_agent_id"],
                        recipient["slot_id"],
                        recipient["issuer_id"],
                        self.events.node_id,
                        payload["timestamp"],
                        payload["timestamp"],
                        stamp,
                    ),
                )
            ).fetchone()
            known = await (
                await db.execute(
                    "SELECT 1 FROM mcp_event_records WHERE event_id=? AND origin_node_id=? "
                    "AND recipient_agent_id=?",
                    (payload["eventId"], peer, recipient["recipient_agent_id"]),
                )
            ).fetchone()
            if not active or payload["timestamp"] < horizon or known:
                await db.commit()
                return {"ok": True, "complete": True}
            previous = await (
                await db.execute(
                    "SELECT metadata_json,data FROM mcp_event_mesh_message_chunks "
                    "WHERE source_node_id=? AND event_id=? AND part_index=?",
                    (peer, payload["eventId"], payload["index"]),
                )
            ).fetchone()
            all_metadata = await (
                await db.execute(
                    "SELECT metadata_json FROM mcp_event_mesh_message_chunks "
                    "WHERE source_node_id=? AND event_id=? LIMIT 1",
                    (peer, payload["eventId"]),
                )
            ).fetchone()
            if (all_metadata and all_metadata[0] != encoded) or (
                previous and (previous[0] != encoded or previous[1] != part)
            ):
                raise ValueError("message chunk identity conflict")
            await db.execute(
                "INSERT OR IGNORE INTO mcp_event_mesh_message_chunks VALUES(?,?,?,?,?,?)",
                (peer, payload["eventId"], payload["index"], encoded, part, stamp),
            )
            rows = await (
                await db.execute(
                    "SELECT data FROM mcp_event_mesh_message_chunks WHERE source_node_id=? "
                    "AND event_id=? ORDER BY part_index",
                    (peer, payload["eventId"]),
                )
            ).fetchall()
            if len(rows) == payload["total"]:
                assembled = b"".join(row[0] for row in rows)
                if (
                    len(assembled) > PRIVATE_EVENT_BYTES
                    or hashlib.sha256(assembled).hexdigest() != payload["digest"]
                ):
                    raise ValueError("invalid message chunk digest")
            await db.commit()
        if assembled is not None:
            event = json.loads(assembled)
            if (
                event.get("eventId") != payload["eventId"]
                or event.get("timestamp") != payload["timestamp"]
                or event.get("recipient") != recipient
            ):
                raise ValueError("message chunk metadata mismatch")
            await self.receive_events(peer, {"events": [event]})
            async with aiosqlite.connect(self.events.path, timeout=5) as db:
                await db.execute(
                    "DELETE FROM mcp_event_mesh_message_chunks "
                    "WHERE source_node_id=? AND event_id=?",
                    (peer, payload["eventId"]),
                )
                await db.commit()
        return {"ok": True, "complete": assembled is not None}

    async def due(self, peer):
        stamp = utc_text()
        async with aiosqlite.connect(self.events.path, timeout=5) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await self.backfill(db, peer, stamp)
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
                size = len(wire_bytes({"events": items + [item]}))
                if size > 64 * 1024 and items:
                    break
                if len(wire_bytes(item)) > PRIVATE_EVENT_BYTES:
                    raise ValueError("message event exceeds legal source envelope budget")
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
                progress = self._interest_progress.get(peer.instance_id)
                offset = progress[1] if progress and progress[0] == snapshot["version"] else 0
                page = snapshot["recipients"][offset : offset + RECIPIENT_PAGE]
                complete = offset + len(page) >= len(snapshot["recipients"])
                result = await self.lifecycle.request(
                    peer,
                    "message-events/interest",
                    {
                        "version": snapshot["version"],
                        "recipients": page,
                        "offset": offset,
                        "complete": complete,
                    },
                )
                expected = offset + len(page)
                if (
                    result.get("ok") is not True
                    or type(result.get("next_offset")) is not int
                    or result["next_offset"] < expected
                    or type(result.get("complete")) is not bool
                ):
                    raise ValueError("message interest page rejected")
                if result["complete"]:
                    self._announced[peer.instance_id] = (snapshot["version"], current + 120)
                    self._interest_progress.pop(peer.instance_id, None)
                else:
                    self._interest_progress[peer.instance_id] = (
                        snapshot["version"],
                        result["next_offset"],
                    )
            pending = await self.due(peer.instance_id)
            if pending:
                if len(wire_bytes({"events": pending})) > 64 * 1024:
                    item = pending[0]
                    raw = wire_bytes(item)
                    total = (len(raw) + CHUNK_BYTES - 1) // CHUNK_BYTES
                    digest = hashlib.sha256(raw).hexdigest()
                    for index in range(total):
                        result = await self.lifecycle.request(
                            peer,
                            "message-events/chunk",
                            {
                                "eventId": item["eventId"],
                                "timestamp": item["timestamp"],
                                "recipient": item["recipient"],
                                "digest": digest,
                                "index": index,
                                "total": total,
                                "part": base64.b64encode(
                                    raw[index * CHUNK_BYTES : (index + 1) * CHUNK_BYTES]
                                ).decode(),
                            },
                        )
                        if result.get("ok") is not True:
                            raise ValueError("message event chunk rejected")
                        if result.get("complete") is True:
                            break
                    if result.get("complete") is not True:
                        raise ValueError("message event assembly pending")
                    result = {"ok": True, "acked": [item["eventId"]]}
                else:
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


def build_message_event_router(messages, auth, mesh):
    router = APIRouter()

    def authenticate(peer_id, authorization, payload):
        peer_id = authenticate_access_mesh_peer(mesh, auth, peer_id, authorization)
        if len(wire_bytes(payload)) > 64 * 1024:
            raise HTTPException(413, "message event payload exceeds byte budget")
        return peer_id

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

    @router.post("/internal/fleet/access-mesh/message-events/chunk", include_in_schema=False)
    async def chunk(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization, payload)
        try:
            return await messages.receive_chunk(peer, payload)
        except (ValueError, TypeError):
            raise HTTPException(400, "invalid message event chunk") from None

    return router
