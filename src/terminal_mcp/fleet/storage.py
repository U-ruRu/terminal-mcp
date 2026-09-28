from __future__ import annotations

import json
from dataclasses import asdict

import aiosqlite

from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.fleet.identity import AgentIdentityRecord, SignedAgentIdentity


class FleetIdentityStore:
    def __init__(self, path):
        self.path = path

    @staticmethod
    def _record_json(record: AgentIdentityRecord) -> str:
        return json.dumps(asdict(record), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _envelope(row) -> SignedAgentIdentity:
        return SignedAgentIdentity(
            AgentIdentityRecord(**json.loads(row[0])),
            row[1],
        )

    async def apply(self, envelope: SignedAgentIdentity, received_at: str | None = None) -> str:
        record = envelope.record
        record_json = self._record_json(record)
        stamp = received_at or utc_text()
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT revision,record_json,signature FROM fleet_agent_identities "
                    "WHERE source_instance_id=? AND agent_id=?",
                    (record.source_instance_id, record.agent_id),
                )
            ).fetchone()
            if row is not None:
                current_revision = int(row[0])
                current_record = AgentIdentityRecord(**json.loads(row[1]))
                if record.revision < current_revision:
                    await db.commit()
                    return "stale"
                if record.revision == current_revision:
                    await db.commit()
                    if row[1] == record_json and row[2] == envelope.signature:
                        return "duplicate"
                    return "conflict"
                if (
                    record.session_started_at != current_record.session_started_at
                    or record.expires_at != current_record.expires_at
                ):
                    await db.commit()
                    return "conflict"
                if current_record.state != "active" and (
                    record.state != current_record.state
                    or record.ended_at != current_record.ended_at
                    or record.end_reason != current_record.end_reason
                ):
                    await db.commit()
                    return "conflict"
                await db.execute(
                    "UPDATE fleet_agent_identities SET revision=?,record_json=?,signature=?,"
                    "received_at=? WHERE source_instance_id=? AND agent_id=?",
                    (
                        record.revision,
                        record_json,
                        envelope.signature,
                        stamp,
                        record.source_instance_id,
                        record.agent_id,
                    ),
                )
            else:
                await db.execute(
                    "INSERT INTO fleet_agent_identities("
                    "source_instance_id,agent_id,revision,record_json,signature,received_at"
                    ") VALUES(?,?,?,?,?,?)",
                    (
                        record.source_instance_id,
                        record.agent_id,
                        record.revision,
                        record_json,
                        envelope.signature,
                        stamp,
                    ),
                )
            await db.commit()
        return "applied"

    async def get(self, source_instance_id: str, agent_id: str) -> SignedAgentIdentity | None:
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            row = await (
                await db.execute(
                    "SELECT record_json,signature FROM fleet_agent_identities "
                    "WHERE source_instance_id=? AND agent_id=?",
                    (source_instance_id, agent_id),
                )
            ).fetchone()
        return self._envelope(row) if row else None

    async def list(
        self, *, agent_id: str | None = None, limit: int = 500
    ) -> list[SignedAgentIdentity]:
        limit = max(1, min(int(limit), 1000))
        condition = ""
        params: list[object] = []
        if agent_id:
            condition = "WHERE agent_id=?"
            params.append(agent_id)
        params.append(limit)
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (
                await db.execute(
                    "SELECT record_json,signature FROM fleet_agent_identities "
                    f"{condition} ORDER BY received_at DESC LIMIT ?",
                    params,
                )
            ).fetchall()
        return [self._envelope(row) for row in rows]

    async def enqueue(
        self,
        peer_instance_ids: list[str],
        envelope: SignedAgentIdentity,
        queued_at: str | None = None,
    ) -> None:
        if not peer_instance_ids:
            return
        record = envelope.record
        record_json = self._record_json(record)
        stamp = queued_at or utc_text()
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            await db.executemany(
                "INSERT INTO fleet_peer_outbox("
                "peer_instance_id,source_instance_id,agent_id,revision,record_json,signature,"
                "queued_at,attempt_count,last_attempt_at,last_error"
                ") VALUES(?,?,?,?,?,?,?,0,NULL,NULL) "
                "ON CONFLICT(peer_instance_id,source_instance_id,agent_id) DO UPDATE SET "
                "revision=excluded.revision,record_json=excluded.record_json,"
                "signature=excluded.signature,queued_at=excluded.queued_at,"
                "attempt_count=0,last_attempt_at=NULL,last_error=NULL "
                "WHERE excluded.revision > fleet_peer_outbox.revision",
                [
                    (
                        peer_id,
                        record.source_instance_id,
                        record.agent_id,
                        record.revision,
                        record_json,
                        envelope.signature,
                        stamp,
                    )
                    for peer_id in peer_instance_ids
                ],
            )
            await db.commit()

    async def pending(self, peer_instance_id: str, limit: int = 100) -> list[SignedAgentIdentity]:
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (
                await db.execute(
                    "SELECT record_json,signature FROM fleet_peer_outbox "
                    "WHERE peer_instance_id=? "
                    "ORDER BY queued_at,source_instance_id,agent_id LIMIT ?",
                    (peer_instance_id, max(1, min(int(limit), 500))),
                )
            ).fetchall()
        return [self._envelope(row) for row in rows]

    async def delivery_result(
        self,
        peer_instance_id: str,
        envelope: SignedAgentIdentity,
        *,
        error: str | None,
        attempted_at: str | None = None,
    ) -> None:
        record = envelope.record
        stamp = attempted_at or utc_text()
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            if error is None:
                await db.execute(
                    "DELETE FROM fleet_peer_outbox WHERE peer_instance_id=? "
                    "AND source_instance_id=? AND agent_id=? AND revision=?",
                    (
                        peer_instance_id,
                        record.source_instance_id,
                        record.agent_id,
                        record.revision,
                    ),
                )
            else:
                await db.execute(
                    "UPDATE fleet_peer_outbox SET attempt_count=attempt_count+1,"
                    "last_attempt_at=?,last_error=? WHERE peer_instance_id=? "
                    "AND source_instance_id=? AND agent_id=? AND revision=?",
                    (
                        stamp,
                        error[:500],
                        peer_instance_id,
                        record.source_instance_id,
                        record.agent_id,
                        record.revision,
                    ),
                )
            await db.commit()

    async def outbox_count(self, peer_instance_id: str | None = None) -> int:
        condition = ""
        params: tuple[object, ...] = ()
        if peer_instance_id:
            condition = " WHERE peer_instance_id=?"
            params = (peer_instance_id,)
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            row = await (
                await db.execute(f"SELECT COUNT(*) FROM fleet_peer_outbox{condition}", params)
            ).fetchone()
        return int(row[0])
