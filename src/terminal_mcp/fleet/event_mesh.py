"""Subscription-derived lifecycle interest and durable node-to-node delivery.

Only locally sourced public lifecycle records enter the peer outbox. Callback
URLs, signing keys, recipient bindings and message contents never enter this wire.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta

import aiosqlite
from fastapi import APIRouter, Header, HTTPException

from terminal_mcp.core.orchestration import parse_utc, utc_now, utc_text

LIFECYCLE_NAMES = ("agent.attached", "agent.inactive", "session.ended")
INTEREST_LEASE_SECONDS = 600
INTEREST_RENEW_SECONDS = 120
MESH_POLL_SECONDS = 5
MESH_RETENTION_SECONDS = 86400
MAX_PEER_ATTEMPTS = 288
MAX_BATCH = 20
MAX_WIRE_BYTES = 64 * 1024

MESH_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_event_mesh_interest(
    peer_node_id TEXT NOT NULL,
    name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY(peer_node_id,name)
);
CREATE TABLE IF NOT EXISTS mcp_event_mesh_versions(
    peer_node_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_event_mesh_local_version(
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version INTEGER NOT NULL,
    names_json TEXT NOT NULL,
    starts_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS mcp_event_mesh_outbox(
    event_id TEXT NOT NULL,
    peer_node_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(event_id,peer_node_id)
);
CREATE INDEX IF NOT EXISTS ix_mcp_event_mesh_outbox_due
    ON mcp_event_mesh_outbox(peer_node_id,state,next_attempt_at);
"""


async def initialize_mesh_storage(db):
    from terminal_mcp.fleet.message_event_mesh import MESSAGE_SCHEMA

    await db.executescript(MESH_SCHEMA + MESSAGE_SCHEMA)
    columns = {
        row[1]
        for row in await (
            await db.execute("PRAGMA table_info(mcp_event_mesh_local_version)")
        ).fetchall()
    }
    if "starts_json" not in columns:
        await db.execute(
            "ALTER TABLE mcp_event_mesh_local_version "
            "ADD COLUMN starts_json TEXT NOT NULL DEFAULT '{}'"
        )
    columns = {
        row[1]
        for row in await (await db.execute("PRAGMA table_info(mcp_event_records)")).fetchall()
    }
    if "origin_node_id" not in columns:
        await db.execute("ALTER TABLE mcp_event_records ADD COLUMN origin_node_id TEXT")
    await db.execute("UPDATE mcp_event_mesh_outbox SET state='pending' WHERE state='sending'")


async def queue_lifecycle_events(db, stamp):
    """Queue at most one durable network delivery per source event / Access node."""
    horizon = utc_text(parse_utc(stamp) - timedelta(seconds=MESH_RETENTION_SECONDS))
    await db.execute(
        "INSERT OR IGNORE INTO mcp_event_mesh_outbox"
        "(event_id,peer_node_id,next_attempt_at,created_at) "
        "SELECT e.event_id,i.peer_node_id,?,? FROM mcp_event_records e "
        "JOIN mcp_event_mesh_interest i ON i.name=e.name "
        "WHERE e.origin_node_id IS NULL AND i.expires_at>? "
        "AND e.occurred_at>=i.started_at AND e.occurred_at<? AND e.occurred_at>=?",
        (stamp, stamp, stamp, stamp, horizon),
    )
    await db.execute(
        "UPDATE mcp_event_mesh_outbox SET state='dead' WHERE state IN ('pending','sending') "
        "AND event_id IN(SELECT event_id FROM mcp_event_records "
        "WHERE name!='message.received') "
        "AND NOT EXISTS(SELECT 1 FROM mcp_event_mesh_interest i "
        "JOIN mcp_event_records e ON e.name=i.name "
        "WHERE e.event_id=mcp_event_mesh_outbox.event_id "
        "AND i.peer_node_id=mcp_event_mesh_outbox.peer_node_id)",
    )


async def prune_mesh_storage(db, current):
    expired = utc_text(current - timedelta(seconds=MESH_RETENTION_SECONDS))
    await db.execute("DELETE FROM mcp_event_mesh_outbox WHERE created_at<?", (expired,))
    # Imported records are the dedup ledger. Local source retention is unchanged.
    await db.execute(
        "DELETE FROM mcp_event_deliveries WHERE event_id IN ("
        "SELECT event_id FROM mcp_event_records WHERE origin_node_id IS NOT NULL "
        "AND occurred_at<?)",
        (expired,),
    )
    await db.execute(
        "DELETE FROM mcp_event_records WHERE origin_node_id IS NOT NULL AND occurred_at<?",
        (expired,),
    )


class LifecycleEventMesh:
    """Reuse AccessMeshReplication's pinned trust, HTTP client, wake and retry loop."""

    def __init__(self, events, request, *, authorization_checker=None):
        self.events = events
        self.request = request
        self.authorization_checker = authorization_checker
        self._announced = {}
        self._snapshot_lock = asyncio.Lock()
        self.peer_health = {}
        from terminal_mcp.fleet.message_event_mesh import MessageEventMesh

        self.message_events = MessageEventMesh(self)

    async def local_interest(self, *, now=None):
        async with self._snapshot_lock:
            return await self._local_interest(now=now)

    async def _local_interest(self, *, now=None):
        stamp = utc_text(now or utc_now())
        async with aiosqlite.connect(self.events.path, timeout=5) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT name,principal_id,credential_id,auth_mode,created_at "
                    "FROM mcp_event_subscriptions WHERE (expires_at IS NULL OR expires_at>?) "
                    "AND name IN ('agent.attached','agent.inactive','session.ended')",
                    (stamp,),
                )
            ).fetchall()
        names = set()
        starts = {}
        for row in rows:
            if self.authorization_checker is not None:
                # Transient credential-check failures fail closed. No session
                # liveness check: a persistent subscriber survives a work window.
                if not await self.authorization_checker(dict(row)):
                    continue
            names.add(row["name"])
            starts[row["name"]] = min(starts.get(row["name"], row["created_at"]), row["created_at"])
        async with aiosqlite.connect(self.events.path, timeout=5) as db:
            await db.execute("BEGIN IMMEDIATE")
            encoded = json.dumps(sorted(names), separators=(",", ":"))
            old = await (
                await db.execute(
                    "SELECT version,names_json,starts_json FROM mcp_event_mesh_local_version "
                    "WHERE singleton=1"
                )
            ).fetchone()
            version = (old[0] + (old[1] != encoded)) if old else 1
            previous_starts = json.loads(old[2]) if old else {}
            starts = {name: previous_starts.get(name, starts[name]) for name in names}
            await db.execute(
                "INSERT INTO mcp_event_mesh_local_version VALUES(1,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "version=excluded.version,names_json=excluded.names_json,"
                "starts_json=excluded.starts_json",
                (version, encoded, json.dumps(starts, sort_keys=True)),
            )
            await db.commit()
        return {"version": version, "event_names": sorted(names), "started_at": starts}

    async def receive_interest(self, peer_id, payload, *, now=None):
        if (
            set(payload) != {"version", "event_names", "started_at"}
            or type(payload["version"]) is not int
            or not 1 <= payload["version"] <= 2**63 - 1
            or not isinstance(payload["event_names"], list)
            or len(payload["event_names"]) > len(LIFECYCLE_NAMES)
            or any(name not in LIFECYCLE_NAMES for name in payload["event_names"])
            or len(set(payload["event_names"])) != len(payload["event_names"])
            or not isinstance(payload["started_at"], dict)
            or set(payload["started_at"]) != set(payload["event_names"])
        ):
            raise ValueError("invalid lifecycle interest")
        current = now or utc_now()
        for start in payload["started_at"].values():
            if (
                not isinstance(start, str)
                or len(start) > 40
                or parse_utc(start) > current + timedelta(seconds=60)
            ):
                raise ValueError("invalid lifecycle interest boundary")
        stamp = utc_text(current)
        expires = utc_text(current + timedelta(seconds=INTEREST_LEASE_SECONDS))
        async with aiosqlite.connect(self.events.path, timeout=5) as db:
            await db.execute("BEGIN IMMEDIATE")
            old = await (
                await db.execute(
                    "SELECT version FROM mcp_event_mesh_versions WHERE peer_node_id=?",
                    (peer_id,),
                )
            ).fetchone()
            if old and old[0] > payload["version"]:
                await db.rollback()
                return {"ok": True}
            await db.execute(
                "INSERT INTO mcp_event_mesh_versions VALUES(?,?) "
                "ON CONFLICT(peer_node_id) DO UPDATE SET version=excluded.version",
                (peer_id, payload["version"]),
            )
            for name in LIFECYCLE_NAMES:
                if name in payload["event_names"]:
                    await db.execute(
                        "INSERT INTO mcp_event_mesh_interest VALUES(?,?,?,?) "
                        "ON CONFLICT(peer_node_id,name) DO UPDATE SET "
                        "expires_at=excluded.expires_at",
                        (peer_id, name, payload["started_at"][name], expires),
                    )
                else:
                    await db.execute(
                        "DELETE FROM mcp_event_mesh_interest WHERE peer_node_id=? AND name=?",
                        (peer_id, name),
                    )
            await queue_lifecycle_events(db, stamp)
            await db.commit()
        return {"ok": True}

    @staticmethod
    def validate_event(peer_id, item):
        if not isinstance(item, dict) or set(item) != {"eventId", "name", "timestamp", "data"}:
            raise ValueError("invalid lifecycle event")
        if (
            not isinstance(item["eventId"], str)
            or re.fullmatch(r"evt_[0-9a-f]{32}", item["eventId"]) is None
            or item["name"] not in LIFECYCLE_NAMES
            or not isinstance(item["timestamp"], str)
            or len(item["timestamp"]) > 40
            or not isinstance(item["data"], dict)
        ):
            raise ValueError("invalid lifecycle event")
        parse_utc(item["timestamp"])
        data = item["data"]
        allowed = {"agent_name", "server", "task"}
        if item["name"] == "session.ended":
            allowed.add("reason")
        if (
            set(data) - allowed
            or not {"agent_name", "server"} <= set(data)
            or data["server"] != peer_id
            or any(not isinstance(value, str) or len(value) > 4096 for value in data.values())
            or not data["agent_name"]
            or len(data["agent_name"]) > 160
        ):
            raise ValueError("invalid lifecycle event data")

    async def receive_events(self, peer_id, payload, *, now=None):
        items = payload.get("events")
        if (
            set(payload) != {"events"}
            or not isinstance(items, list)
            or not 1 <= len(items) <= MAX_BATCH
        ):
            raise ValueError("invalid lifecycle event batch")
        # Validate the entire bounded batch before writing any record.
        for item in items:
            self.validate_event(peer_id, item)
        current = now or utc_now()
        stamp = utc_text(current)
        horizon = utc_text(current - timedelta(seconds=MESH_RETENTION_SECONDS))
        async with aiosqlite.connect(self.events.path, timeout=5) as db:
            await db.execute("BEGIN IMMEDIATE")
            for item in items:
                if item["timestamp"] < horizon:
                    continue
                # No replay outside an actual current subscription's lifetime.
                active = await (
                    await db.execute(
                        "SELECT 1 FROM mcp_event_subscriptions WHERE name=? AND created_at<=? "
                        "AND (expires_at IS NULL OR expires_at>?) "
                        "AND (expires_at IS NULL OR expires_at>?) LIMIT 1",
                        (item["name"], item["timestamp"], item["timestamp"], stamp),
                    )
                ).fetchone()
                if not active:
                    continue
                await db.execute(
                    "INSERT OR IGNORE INTO mcp_event_records"
                    "(event_id,source_key,name,occurred_at,data_json,origin_node_id) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        item["eventId"],
                        "mesh:" + peer_id + ":" + item["eventId"],
                        item["name"],
                        item["timestamp"],
                        json.dumps(item["data"], ensure_ascii=False, separators=(",", ":")),
                        peer_id,
                    ),
                )
            await db.commit()
        return {"ok": True, "acked": [item["eventId"] for item in items]}

    async def due(self, peer_id, *, now=None):
        current = now or utc_now()
        stamp = utc_text(current)
        async with aiosqlite.connect(self.events.path, timeout=5) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await queue_lifecycle_events(db, stamp)
            await db.execute(
                "UPDATE mcp_event_mesh_outbox SET state=CASE WHEN attempts>=? "
                "THEN 'dead' ELSE 'pending' END WHERE state='sending' "
                "AND next_attempt_at<=?",
                (MAX_PEER_ATTEMPTS, stamp),
            )
            await prune_mesh_storage(db, current)
            rows = await (
                await db.execute(
                    "SELECT o.event_id,e.name,e.occurred_at,e.data_json "
                    "FROM mcp_event_mesh_outbox o "
                    "JOIN mcp_event_records e ON e.event_id=o.event_id "
                    "WHERE e.name!='message.received' AND o.peer_node_id=? "
                    "AND o.state='pending' AND o.next_attempt_at<=? "
                    "AND o.attempts<? AND EXISTS(SELECT 1 FROM mcp_event_mesh_interest i "
                    "WHERE i.peer_node_id=o.peer_node_id AND i.name=e.name AND i.expires_at>?) "
                    "ORDER BY o.next_attempt_at,o.event_id LIMIT ?",
                    (peer_id, stamp, MAX_PEER_ATTEMPTS, stamp, MAX_BATCH),
                )
            ).fetchall()
            for row in rows:
                await db.execute(
                    "UPDATE mcp_event_mesh_outbox SET state='sending',attempts=attempts+1, "
                    "next_attempt_at=? WHERE event_id=? AND peer_node_id=?",
                    (utc_text(current + timedelta(seconds=120)), row["event_id"], peer_id),
                )
            await db.commit()
        return [
            {
                "eventId": row["event_id"],
                "name": row["name"],
                "timestamp": row["occurred_at"],
                "data": json.loads(row["data_json"]),
            }
            for row in rows
        ]

    async def finish(self, peer_id, event_ids, acked, *, now=None):
        current = now or utc_now()
        async with aiosqlite.connect(self.events.path, timeout=5) as db:
            for event_id in event_ids:
                row = await (
                    await db.execute(
                        "SELECT attempts FROM mcp_event_mesh_outbox "
                        "WHERE event_id=? AND peer_node_id=?",
                        (event_id, peer_id),
                    )
                ).fetchone()
                if row is None:
                    continue
                state = (
                    "sent"
                    if event_id in acked
                    else ("dead" if row[0] >= MAX_PEER_ATTEMPTS else "pending")
                )
                delay = min(300, 2 ** min(row[0], 9))
                await db.execute(
                    "UPDATE mcp_event_mesh_outbox SET state=?,next_attempt_at=? "
                    "WHERE event_id=? AND peer_node_id=?",
                    (state, utc_text(current + timedelta(seconds=delay)), event_id, peer_id),
                )
            await db.commit()

    async def sync_peer(self, peer):
        pending = []
        try:
            snapshot = await self.local_interest()
            version = snapshot["version"]
            clock = asyncio.get_running_loop().time()
            announced = self._announced.get(peer.instance_id)
            if announced is None or announced[0] != version or announced[1] <= clock:
                result = await self.request(peer, "lifecycle/interest", snapshot)
                if result.get("ok") is not True:
                    raise ValueError("lifecycle interest rejected")
                self._announced[peer.instance_id] = (version, clock + INTEREST_RENEW_SECONDS)
            pending = await self.due(peer.instance_id)
            if pending:
                result = await self.request(peer, "lifecycle/events", {"events": pending})
                acked = result.get("acked", [])
                ids = {item["eventId"] for item in pending}
                if (
                    result.get("ok") is not True
                    or not isinstance(acked, list)
                    or any(not isinstance(value, str) for value in acked)
                    or not set(acked) <= ids
                ):
                    raise ValueError("invalid lifecycle acknowledgement")
                await self.finish(peer.instance_id, ids, set(acked))
            self.peer_health[peer.instance_id] = {"status": "healthy"}
        except Exception as exc:
            if pending:
                try:
                    await self.finish(peer.instance_id, [i["eventId"] for i in pending], set())
                except Exception:
                    # A claim lease recovers interrupted writes without killing Mesh.
                    pass
            self._announced.pop(peer.instance_id, None)
            self.peer_health[peer.instance_id] = {
                "status": "degraded",
                "reason": type(exc).__name__,
            }

        await self.message_events.sync_peer(peer)
        if self.message_events.peer_health[peer.instance_id]["status"] == "degraded":
            self.peer_health[peer.instance_id] = self.message_events.peer_health[peer.instance_id]


def build_lifecycle_event_router(lifecycle, replication_auth, trusted_issuers):
    router = APIRouter()

    def authenticate(peer_id, authorization, payload):
        peer = replication_auth.authenticate(peer_id, authorization)
        if peer is None or peer.instance_id not in trusted_issuers:
            raise HTTPException(401, "invalid access mesh peer")
        if len(json.dumps(payload, separators=(",", ":")).encode()) > MAX_WIRE_BYTES:
            raise HTTPException(413, "lifecycle payload exceeds byte budget")
        return peer.instance_id

    @router.post("/internal/fleet/access-mesh/lifecycle/interest", include_in_schema=False)
    async def interest(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer_id = authenticate(x_terminal_mcp_peer, authorization, payload)
        try:
            return await lifecycle.receive_interest(peer_id, payload)
        except (ValueError, TypeError):
            raise HTTPException(400, "invalid lifecycle interest") from None

    @router.post("/internal/fleet/access-mesh/lifecycle/events", include_in_schema=False)
    async def events(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer_id = authenticate(x_terminal_mcp_peer, authorization, payload)
        try:
            return await lifecycle.receive_events(peer_id, payload)
        except (ValueError, TypeError):
            raise HTTPException(400, "invalid lifecycle events") from None

    return router
