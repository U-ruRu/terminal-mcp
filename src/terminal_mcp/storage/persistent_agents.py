from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager

import aiosqlite

from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.core.persistent_agents import (
    PersistentSlot,
    WorkSessionRecord,
)
from terminal_mcp.storage.sqlite_observability import SqliteDiagnostics, observed_connection


class PersistentStoreError(RuntimeError):
    def __init__(self, code: str, *, blockers: list[dict] | None = None):
        self.code = code
        self.blockers = blockers or []
        super().__init__(code)


class PersistentAgentStore:
    """Additive persistence primitives for M3.5 Persistent Slots.

    Slice A deliberately exposes storage operations only. Lifecycle authorization,
    admission and command fencing are owned by Slice B.
    """

    def __init__(self, path):
        self.path = path
        self.sqlite_diagnostics = SqliteDiagnostics("persistent_agent")

    def configure_observability(self, events, metrics) -> None:
        self.sqlite_diagnostics.configure(events, metrics)

    @asynccontextmanager
    async def _connect(self, operation="persistent_agent"):
        async with observed_connection(
            aiosqlite.connect,
            self.path,
            busy_timeout=1.0,
            diagnostics=self.sqlite_diagnostics,
            operation=operation,
            pragmas=("PRAGMA busy_timeout=1000", "PRAGMA foreign_keys=ON"),
        ) as db:
            yield db

    @staticmethod
    def _slot(row) -> PersistentSlot | None:
        if row is None:
            return None
        return PersistentSlot(*row)

    @staticmethod
    def _session(row) -> WorkSessionRecord | None:
        if row is None:
            return None
        return WorkSessionRecord(*row)

    async def get_slot(self, logical_agent_id: str) -> PersistentSlot | None:
        async with self._connect("persistent_slot_get") as db:
            row = await (
                await db.execute(
                    "SELECT logical_agent_id,display_name,state,authority_node_id,authority_epoch,"
                    "slot_revision,selector_generation,auth_generation,created_at,updated_at,"
                    "deleted_at,tombstone_reason FROM logical_agents WHERE logical_agent_id=?",
                    (logical_agent_id,),
                )
            ).fetchone()
        return self._slot(row)

    async def get_work_session(self, work_session_id: str) -> WorkSessionRecord | None:
        async with self._connect("persistent_work_session_get") as db:
            row = await (
                await db.execute(
                    "SELECT work_session_id,logical_agent_id,session_epoch,authority_node_id,"
                    "authority_epoch,started_at,hard_expires_at,auth_principal_id,auth_generation,"
                    "state,origin_instance_id,ended_at,end_reason "
                    "FROM logical_agent_work_sessions WHERE work_session_id=?",
                    (work_session_id,),
                )
            ).fetchone()
        return self._session(row)

    async def active_session_for_slot(self, logical_agent_id: str) -> WorkSessionRecord | None:
        async with self._connect("persistent_session_active") as db:
            row = await (
                await db.execute(
                    "SELECT work_session_id,logical_agent_id,session_epoch,authority_node_id,"
                    "authority_epoch,started_at,hard_expires_at,auth_principal_id,auth_generation,"
                    "state,origin_instance_id,ended_at,end_reason FROM logical_agent_work_sessions "
                    "WHERE logical_agent_id=? AND state IN ('active','stopping') "
                    "ORDER BY session_epoch DESC LIMIT 1",
                    (logical_agent_id,),
                )
            ).fetchone()
        return self._session(row)

    @staticmethod
    def idempotency_fingerprint(payload: dict) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(encoded).hexdigest()

    async def idempotency_get(
        self,
        logical_agent_id: str,
        operation: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> dict | None:
        async with self._connect("persistent_idempotency_get") as db:
            row = await (
                await db.execute(
                    "SELECT request_fingerprint,state,result_json FROM persistent_idempotency "
                    "WHERE logical_agent_id=? AND operation=? AND idempotency_key=?",
                    (logical_agent_id, operation, idempotency_key),
                )
            ).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            raise PersistentStoreError("idempotency_conflict")
        if row[1] == "pending":
            raise PersistentStoreError("idempotency_in_progress")
        if row[1] != "complete" or row[2] is None:
            raise PersistentStoreError("idempotency_conflict")
        return json.loads(row[2])

    async def idempotency_reserve(
        self,
        logical_agent_id: str,
        operation: str,
        idempotency_key: str,
        fingerprint: str,
        *,
        now: str | None = None,
    ) -> dict | None:
        stamp = now or utc_text()
        async with self._connect("persistent_idempotency_reserve") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT request_fingerprint,state,result_json FROM persistent_idempotency "
                        "WHERE logical_agent_id=? AND operation=? AND idempotency_key=?",
                        (logical_agent_id, operation, idempotency_key),
                    )
                ).fetchone()
                if row is not None:
                    if row[0] != fingerprint:
                        raise PersistentStoreError("idempotency_conflict")
                    if row[1] == "pending":
                        raise PersistentStoreError("idempotency_in_progress")
                    if row[1] != "complete" or row[2] is None:
                        raise PersistentStoreError("idempotency_conflict")
                    await db.commit()
                    return json.loads(row[2])
                await db.execute(
                    "INSERT INTO persistent_idempotency("
                    "logical_agent_id,operation,idempotency_key,request_fingerprint,"
                    "state,result_json,created_at) VALUES(?,?,?,?, 'pending',NULL,?)",
                    (logical_agent_id, operation, idempotency_key, fingerprint, stamp),
                )
                await db.commit()
                return None
            except Exception:
                await db.rollback()
                raise

    async def idempotency_complete(
        self,
        logical_agent_id: str,
        operation: str,
        idempotency_key: str,
        fingerprint: str,
        result: dict,
    ) -> dict:
        payload = json.dumps(result, sort_keys=True, separators=(",", ":"), default=str)
        async with self._connect("persistent_idempotency_complete") as db:
            cur = await db.execute(
                "UPDATE persistent_idempotency SET state='complete',result_json=? "
                "WHERE logical_agent_id=? AND operation=? AND idempotency_key=? "
                "AND request_fingerprint=? AND state='pending'",
                (payload, logical_agent_id, operation, idempotency_key, fingerprint),
            )
            await db.commit()
        if cur.rowcount != 1:
            replay = await self.idempotency_get(
                logical_agent_id, operation, idempotency_key, fingerprint
            )
            if replay is None:
                raise PersistentStoreError("idempotency_conflict")
            return replay
        return result

    async def idempotency_abort(
        self,
        logical_agent_id: str,
        operation: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> bool:
        async with self._connect("persistent_idempotency_abort") as db:
            cur = await db.execute(
                "DELETE FROM persistent_idempotency WHERE logical_agent_id=? AND operation=? "
                "AND idempotency_key=? AND request_fingerprint=? AND state='pending'",
                (logical_agent_id, operation, idempotency_key, fingerprint),
            )
            await db.commit()
        return cur.rowcount == 1

    async def idempotency_put(
        self,
        logical_agent_id: str,
        operation: str,
        idempotency_key: str,
        fingerprint: str,
        result: dict,
        *,
        now: str | None = None,
    ) -> dict:
        replay = await self.idempotency_reserve(
            logical_agent_id,
            operation,
            idempotency_key,
            fingerprint,
            now=now,
        )
        if replay is not None:
            return replay
        return await self.idempotency_complete(
            logical_agent_id, operation, idempotency_key, fingerprint, result
        )

    async def add_audit_event(
        self,
        logical_agent_id: str,
        event_type: str,
        *,
        principal_id: str,
        work_session_id: str | None = None,
        session_epoch: int | None = None,
        payload: dict | None = None,
        now: str | None = None,
    ) -> dict:
        stamp = now or utc_text()
        encoded = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)
        async with self._connect("persistent_audit_add") as db:
            cur = await db.execute(
                "INSERT INTO persistent_agent_audit("
                "logical_agent_id,event_type,principal_id,work_session_id,session_epoch,payload_json,created_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    logical_agent_id,
                    event_type,
                    principal_id,
                    work_session_id,
                    session_epoch,
                    encoded,
                    stamp,
                ),
            )
            await db.commit()
        return {
            "id": int(cur.lastrowid),
            "logical_agent_id": logical_agent_id,
            "event_type": event_type,
            "principal_id": principal_id,
            "work_session_id": work_session_id,
            "session_epoch": session_epoch,
            "payload": payload or {},
            "created_at": stamp,
        }

    async def audit_events(self, logical_agent_id: str, *, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        async with self._connect("persistent_audit_list") as db:
            rows = await (
                await db.execute(
                    "SELECT id,event_type,principal_id,work_session_id,session_epoch,"
                    "payload_json,created_at "
                    "FROM persistent_agent_audit WHERE logical_agent_id=? ORDER BY id DESC LIMIT ?",
                    (logical_agent_id, limit),
                )
            ).fetchall()
        return [
            {
                "id": int(row[0]),
                "logical_agent_id": logical_agent_id,
                "event_type": row[1],
                "principal_id": row[2],
                "work_session_id": row[3],
                "session_epoch": int(row[4]) if row[4] is not None else None,
                "payload": json.loads(row[5]),
                "created_at": row[6],
            }
            for row in rows
        ]
