from __future__ import annotations

import json

import aiosqlite

from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.fleet.identity import AgentIdentityRecord, SignedAgentIdentity
from terminal_mcp.storage.sqlite_observability import cancellation_safe_connection


class FleetIdentityStore:
    def __init__(self, path):
        self.path = path

    @staticmethod
    def _record_json(record: AgentIdentityRecord) -> str:
        return json.dumps(
            record.record_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )

    @staticmethod
    def _envelope(row) -> SignedAgentIdentity:
        payload = json.loads(row[0])
        return SignedAgentIdentity(AgentIdentityRecord.from_dict(payload), row[1])

    async def apply(self, envelope: SignedAgentIdentity, received_at: str | None = None) -> str:
        record = envelope.record
        record_json = self._record_json(record)
        stamp = received_at or utc_text()
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
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
                current_record = AgentIdentityRecord.from_dict(json.loads(row[1]))
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
                    idle_recovery = (
                        current_record.payload_version >= 3
                        and record.payload_version >= 3
                        and current_record.state == "forced"
                        and current_record.end_reason == "idle_timeout"
                        and record.state == "active"
                        and current_record.ended_at is not None
                        and record.last_activity_at > current_record.ended_at
                    )
                    if not idle_recovery:
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
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
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
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
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
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
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
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
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
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
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

    async def enqueue_finish(
        self,
        origin_instance_id: str,
        agent_id: str,
        ended_at: str,
        reason: str,
        queued_at: str | None = None,
    ) -> None:
        stamp = queued_at or utc_text()
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            await db.execute(
                "INSERT INTO fleet_finish_outbox("
                "origin_instance_id,agent_id,ended_at,reason,queued_at,"
                "attempt_count,last_attempt_at,last_error"
                ") VALUES(?,?,?,?,?,0,NULL,NULL) "
                "ON CONFLICT(origin_instance_id,agent_id) DO UPDATE SET "
                "ended_at=excluded.ended_at,reason=excluded.reason,queued_at=excluded.queued_at,"
                "attempt_count=0,last_attempt_at=NULL,last_error=NULL",
                (origin_instance_id, agent_id, ended_at, reason, stamp),
            )
            await db.commit()

    async def pending_finishes(self, limit: int = 100) -> list[dict]:
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            rows = await (
                await db.execute(
                    "SELECT origin_instance_id,agent_id,ended_at,reason,queued_at,"
                    "attempt_count,last_attempt_at,last_error FROM fleet_finish_outbox "
                    "ORDER BY queued_at,origin_instance_id,agent_id LIMIT ?",
                    (max(1, min(int(limit), 500)),),
                )
            ).fetchall()
        return [
            {
                "origin_instance_id": row[0],
                "agent_id": row[1],
                "ended_at": row[2],
                "reason": row[3],
                "queued_at": row[4],
                "attempt_count": int(row[5]),
                "last_attempt_at": row[6],
                "last_error": row[7],
            }
            for row in rows
        ]

    async def finish_delivery_result(
        self,
        origin_instance_id: str,
        agent_id: str,
        *,
        error: str | None,
        attempted_at: str | None = None,
    ) -> None:
        stamp = attempted_at or utc_text()
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            if error is None:
                await db.execute(
                    "DELETE FROM fleet_finish_outbox WHERE origin_instance_id=? AND agent_id=?",
                    (origin_instance_id, agent_id),
                )
            else:
                await db.execute(
                    "UPDATE fleet_finish_outbox SET attempt_count=attempt_count+1,"
                    "last_attempt_at=?,last_error=? "
                    "WHERE origin_instance_id=? AND agent_id=?",
                    (stamp, error[:500], origin_instance_id, agent_id),
                )
            await db.commit()

    async def enqueue_session_update(
        self,
        origin_instance_id,
        agent_id,
        source_instance_id,
        activity_at,
        *,
        intent=None,
        intent_step=None,
        intent_updated_at=None,
        queued_at=None,
    ) -> None:
        stamp = queued_at or utc_text()
        intent_is_newer = (
            "excluded.intent_updated_at IS NOT NULL AND "
            "(fleet_session_update_outbox.intent_updated_at IS NULL OR "
            "excluded.intent_updated_at>=fleet_session_update_outbox.intent_updated_at)"
        )
        sql = (
            "INSERT INTO fleet_session_update_outbox("
            "origin_instance_id,agent_id,source_instance_id,activity_at,intent,intent_step,"
            "intent_updated_at,queued_at,attempt_count,last_attempt_at,last_error) "
            "VALUES(?,?,?,?,?,?,?,?,0,NULL,NULL) "
            "ON CONFLICT(origin_instance_id,agent_id,source_instance_id) DO UPDATE SET "
            "activity_at=MAX(fleet_session_update_outbox.activity_at,excluded.activity_at),"
            f"intent=CASE WHEN {intent_is_newer} THEN excluded.intent "
            "ELSE fleet_session_update_outbox.intent END,"
            f"intent_step=CASE WHEN {intent_is_newer} THEN excluded.intent_step "
            "ELSE fleet_session_update_outbox.intent_step END,"
            f"intent_updated_at=CASE WHEN {intent_is_newer} THEN excluded.intent_updated_at "
            "ELSE fleet_session_update_outbox.intent_updated_at END,"
            "queued_at=excluded.queued_at,attempt_count=0,"
            "last_attempt_at=NULL,last_error=NULL"
        )
        params = (
            origin_instance_id,
            agent_id,
            source_instance_id,
            activity_at,
            intent,
            intent_step,
            intent_updated_at,
            stamp,
        )
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            await db.execute(sql, params)
            await db.commit()

    async def pending_session_updates(self, limit=100):
        sql = (
            "SELECT origin_instance_id,agent_id,source_instance_id,activity_at,intent,"
            "intent_step,intent_updated_at,queued_at,attempt_count,last_attempt_at,last_error "
            "FROM fleet_session_update_outbox "
            "ORDER BY queued_at,origin_instance_id,agent_id LIMIT ?"
        )
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            rows = await (await db.execute(sql, (max(1, min(int(limit), 500)),))).fetchall()
        keys = (
            "origin_instance_id",
            "agent_id",
            "source_instance_id",
            "activity_at",
            "intent",
            "intent_step",
            "intent_updated_at",
            "queued_at",
            "attempt_count",
            "last_attempt_at",
            "last_error",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    async def session_update_delivery_result(
        self,
        origin_instance_id,
        agent_id,
        source_instance_id,
        *,
        error,
        attempted_at=None,
    ):
        stamp = attempted_at or utc_text()
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            if error is None:
                await db.execute(
                    "DELETE FROM fleet_session_update_outbox "
                    "WHERE origin_instance_id=? AND agent_id=? AND source_instance_id=?",
                    (origin_instance_id, agent_id, source_instance_id),
                )
            else:
                await db.execute(
                    "UPDATE fleet_session_update_outbox "
                    "SET attempt_count=attempt_count+1,last_attempt_at=?,last_error=? "
                    "WHERE origin_instance_id=? AND agent_id=? AND source_instance_id=?",
                    (
                        stamp,
                        error[:500],
                        origin_instance_id,
                        agent_id,
                        source_instance_id,
                    ),
                )
            await db.commit()

    async def session_update_outbox_count(self) -> int:
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            row = await (
                await db.execute("SELECT COUNT(*) FROM fleet_session_update_outbox")
            ).fetchone()
        return int(row[0])

    async def finish_outbox_count(self) -> int:
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            row = await (await db.execute("SELECT COUNT(*) FROM fleet_finish_outbox")).fetchone()
        return int(row[0])

    async def outbox_count(self, peer_instance_id: str | None = None) -> int:
        condition = ""
        params: tuple[object, ...] = ()
        if peer_instance_id:
            condition = " WHERE peer_instance_id=?"
            params = (peer_instance_id,)
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            row = await (
                await db.execute(f"SELECT COUNT(*) FROM fleet_peer_outbox{condition}", params)
            ).fetchone()
        return int(row[0])
