from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import timedelta

import aiosqlite

from terminal_mcp.core.orchestration import parse_utc, utc_text
from terminal_mcp.core.persistent_agents import (
    ArmGeneration,
    PersistentSlot,
    WorkSessionRecord,
    normalize_slot_selector,
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
    def _arm(row) -> ArmGeneration | None:
        if row is None:
            return None
        return ArmGeneration(*row)

    @staticmethod
    def _session(row) -> WorkSessionRecord | None:
        if row is None:
            return None
        return WorkSessionRecord(*row)

    async def create_slot(
        self,
        logical_agent_id: str,
        display_name: str,
        selector: str,
        *,
        authority_node_id: str,
        authority_epoch: int = 1,
        auth_generation: int = 1,
        now: str | None = None,
    ) -> PersistentSlot:
        selector = normalize_slot_selector(selector)
        if authority_epoch < 1 or auth_generation < 1:
            raise ValueError("authority_epoch and auth_generation must be positive")
        stamp = now or utc_text()
        async with self._connect("persistent_slot_create") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "INSERT INTO logical_agents("
                    "logical_agent_id,display_name,state,authority_node_id,authority_epoch,"
                    "slot_revision,selector_generation,auth_generation,created_at,updated_at"
                    ") VALUES(?,?,'suspended',?,?,1,1,?,?,?)",
                    (
                        logical_agent_id,
                        display_name,
                        authority_node_id,
                        authority_epoch,
                        auth_generation,
                        stamp,
                        stamp,
                    ),
                )
                await db.execute(
                    "INSERT INTO logical_agent_selectors("
                    "selector,logical_agent_id,generation,created_at,retired_at,tombstoned_at"
                    ") VALUES(?,?,1,?,NULL,NULL)",
                    (selector, logical_agent_id, stamp),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.get_slot(logical_agent_id)

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

    async def selector_owner(self, selector: str):
        selector = normalize_slot_selector(selector)
        async with self._connect("persistent_selector_lookup") as db:
            row = await (
                await db.execute(
                    "SELECT logical_agent_id,generation,created_at,retired_at,tombstoned_at "
                    "FROM logical_agent_selectors WHERE selector=?",
                    (selector,),
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "logical_agent_id": row[0],
            "generation": int(row[1]),
            "created_at": row[2],
            "retired_at": row[3],
            "tombstoned_at": row[4],
        }

    async def rotate_selector(
        self,
        logical_agent_id: str,
        selector: str,
        *,
        expected_generation: int,
        now: str | None = None,
    ) -> int:
        selector = normalize_slot_selector(selector)
        stamp = now or utc_text()
        async with self._connect("persistent_selector_rotate") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT selector_generation FROM logical_agents WHERE logical_agent_id=?",
                        (logical_agent_id,),
                    )
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown logical agent: {logical_agent_id}")
                if int(row[0]) != int(expected_generation):
                    raise ValueError("selector generation conflict")
                next_generation = int(row[0]) + 1
                await db.execute(
                    "UPDATE logical_agent_selectors SET retired_at=?,tombstoned_at=? "
                    "WHERE logical_agent_id=? AND generation=? AND retired_at IS NULL",
                    (stamp, stamp, logical_agent_id, expected_generation),
                )
                await db.execute(
                    "INSERT INTO logical_agent_selectors("
                    "selector,logical_agent_id,generation,created_at,retired_at,tombstoned_at"
                    ") VALUES(?,?,?,?,NULL,NULL)",
                    (selector, logical_agent_id, next_generation, stamp),
                )
                await db.execute(
                    "UPDATE logical_agents SET selector_generation=?,slot_revision=slot_revision+1,"
                    "updated_at=? WHERE logical_agent_id=?",
                    (next_generation, stamp, logical_agent_id),
                )
                await db.commit()
                return next_generation
            except Exception:
                await db.rollback()
                raise

    async def record_arm(self, arm: ArmGeneration) -> ArmGeneration:
        if arm.generation < 1 or arm.captured_duration_seconds < 1:
            raise ValueError("arm generation and captured duration must be positive")
        async with self._connect("persistent_arm_record") as db:
            await db.execute(
                "INSERT INTO logical_agent_arms("
                "logical_agent_id,generation,armed_at,armed_until,captured_duration_seconds,"
                "selector_generation,auth_generation,slot_revision,consumed_at,revoked_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    arm.logical_agent_id,
                    arm.generation,
                    arm.armed_at,
                    arm.armed_until,
                    arm.captured_duration_seconds,
                    arm.selector_generation,
                    arm.auth_generation,
                    arm.slot_revision,
                    arm.consumed_at,
                    arm.revoked_at,
                ),
            )
            await db.commit()
        return arm

    async def get_arm(self, logical_agent_id: str, generation: int) -> ArmGeneration | None:
        async with self._connect("persistent_arm_get") as db:
            row = await (
                await db.execute(
                    "SELECT logical_agent_id,generation,armed_at,armed_until,"
                    "captured_duration_seconds,selector_generation,auth_generation,slot_revision,"
                    "consumed_at,revoked_at "
                    "FROM logical_agent_arms WHERE logical_agent_id=? AND generation=?",
                    (logical_agent_id, generation),
                )
            ).fetchone()
        return self._arm(row)

    async def record_work_session(self, session: WorkSessionRecord) -> WorkSessionRecord:
        if session.session_epoch < 1 or session.authority_epoch < 1 or session.auth_generation < 1:
            raise ValueError("session and authority generations must be positive")
        async with self._connect("persistent_work_session_record") as db:
            await db.execute(
                "INSERT INTO logical_agent_work_sessions("
                "work_session_id,logical_agent_id,session_epoch,authority_node_id,authority_epoch,"
                "started_at,hard_expires_at,auth_principal_id,auth_generation,state,origin_instance_id,"
                "ended_at,end_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session.work_session_id,
                    session.logical_agent_id,
                    session.session_epoch,
                    session.authority_node_id,
                    session.authority_epoch,
                    session.started_at,
                    session.hard_expires_at,
                    session.auth_principal_id,
                    session.auth_generation,
                    session.state,
                    session.origin_instance_id,
                    session.ended_at,
                    session.end_reason,
                ),
            )
            await db.commit()
        return session

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

    async def persistent_state_exists(self) -> bool:
        async with self._connect("persistent_state_probe") as db:
            slot = await (await db.execute("SELECT 1 FROM logical_agents LIMIT 1")).fetchone()
            if slot is not None:
                return True
            claim = await (
                await db.execute(
                    "SELECT 1 FROM work_claims WHERE owner_kind='logical_agent' LIMIT 1"
                )
            ).fetchone()
        return claim is not None

    async def list_slots(self, *, include_deleted: bool = False) -> list[PersistentSlot]:
        where = "" if include_deleted else " WHERE state<>'deleted'"
        async with self._connect("persistent_slot_list") as db:
            rows = await (
                await db.execute(
                    "SELECT logical_agent_id,display_name,state,authority_node_id,authority_epoch,"
                    "slot_revision,selector_generation,auth_generation,created_at,updated_at,"
                    "deleted_at,tombstone_reason FROM logical_agents"
                    + where
                    + " ORDER BY created_at,logical_agent_id"
                )
            ).fetchall()
        return [self._slot(row) for row in rows]

    async def active_selector(self, logical_agent_id: str) -> dict | None:
        async with self._connect("persistent_selector_active") as db:
            row = await (
                await db.execute(
                    "SELECT s.selector,s.generation,s.created_at FROM logical_agent_selectors s "
                    "JOIN logical_agents a ON a.logical_agent_id=s.logical_agent_id "
                    "WHERE s.logical_agent_id=? AND s.generation=a.selector_generation "
                    "AND s.retired_at IS NULL AND s.tombstoned_at IS NULL",
                    (logical_agent_id,),
                )
            ).fetchone()
        return (
            {"selector": row[0], "generation": int(row[1]), "created_at": row[2]} if row else None
        )

    async def rename_slot(
        self,
        logical_agent_id: str,
        display_name: str,
        *,
        expected_revision: int,
        now: str | None = None,
    ) -> PersistentSlot:
        name = display_name.strip()
        if not name:
            raise ValueError("display name is required")
        stamp = now or utc_text()
        async with self._connect("persistent_slot_rename") as db:
            cur = await db.execute(
                "UPDATE logical_agents SET display_name=?,"
                "slot_revision=slot_revision+1,updated_at=? "
                "WHERE logical_agent_id=? AND slot_revision=? AND state<>'deleted'",
                (name, stamp, logical_agent_id, expected_revision),
            )
            await db.commit()
        if cur.rowcount != 1:
            if await self.get_slot(logical_agent_id) is None:
                raise PersistentStoreError("slot_not_found")
            raise PersistentStoreError("revision_conflict")
        return await self.get_slot(logical_agent_id)

    async def arm_slot(
        self,
        logical_agent_id: str,
        duration_seconds: int,
        *,
        expected_revision: int,
        now: str | None = None,
    ) -> tuple[PersistentSlot, ArmGeneration]:
        if duration_seconds < 1:
            raise ValueError("duration_seconds must be positive")
        stamp = now or utc_text()
        armed_until = utc_text(parse_utc(stamp) + timedelta(seconds=duration_seconds))
        async with self._connect("persistent_slot_arm") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT state,slot_revision,selector_generation,auth_generation "
                        "FROM logical_agents WHERE logical_agent_id=?",
                        (logical_agent_id,),
                    )
                ).fetchone()
                if row is None or row[0] == "deleted":
                    raise PersistentStoreError("slot_not_found")
                if int(row[1]) != int(expected_revision):
                    raise PersistentStoreError("revision_conflict")
                if row[0] in {"active", "stopping", "deleting"}:
                    raise PersistentStoreError(
                        "session_already_active" if row[0] == "active" else "session_stopping"
                    )
                generation_row = await (
                    await db.execute(
                        "SELECT COALESCE(MAX(generation),0)+1 FROM logical_agent_arms "
                        "WHERE logical_agent_id=?",
                        (logical_agent_id,),
                    )
                ).fetchone()
                generation = int(generation_row[0])
                await db.execute(
                    "UPDATE logical_agent_arms SET revoked_at=COALESCE(revoked_at,?) "
                    "WHERE logical_agent_id=? AND consumed_at IS NULL AND revoked_at IS NULL",
                    (stamp, logical_agent_id),
                )
                next_revision = int(row[1]) + 1
                arm = ArmGeneration(
                    logical_agent_id,
                    generation,
                    stamp,
                    armed_until,
                    duration_seconds,
                    int(row[2]),
                    int(row[3]),
                    next_revision,
                )
                await db.execute(
                    "INSERT INTO logical_agent_arms("
                    "logical_agent_id,generation,armed_at,armed_until,captured_duration_seconds,"
                    "selector_generation,auth_generation,slot_revision,consumed_at,revoked_at"
                    ") VALUES(?,?,?,?,?,?,?,?,NULL,NULL)",
                    (
                        arm.logical_agent_id,
                        arm.generation,
                        arm.armed_at,
                        arm.armed_until,
                        arm.captured_duration_seconds,
                        arm.selector_generation,
                        arm.auth_generation,
                        arm.slot_revision,
                    ),
                )
                await db.execute(
                    "UPDATE logical_agents SET state='armed',slot_revision=?,updated_at=? "
                    "WHERE logical_agent_id=?",
                    (next_revision, stamp, logical_agent_id),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.get_slot(logical_agent_id), arm

    async def start_session(
        self,
        *,
        selector: str,
        work_session_id: str,
        expected_revision: int,
        principal_id: str,
        auth_generation: int,
        authority_node_id: str,
        origin_instance_id: str | None,
        now: str | None = None,
    ) -> tuple[PersistentSlot, WorkSessionRecord]:
        selector = normalize_slot_selector(selector)
        stamp = now or utc_text()
        current = parse_utc(stamp)
        async with self._connect("persistent_session_start") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT a.logical_agent_id,a.state,a.authority_node_id,a.authority_epoch,"
                        "a.slot_revision,a.selector_generation,a.auth_generation "
                        "FROM logical_agents a JOIN logical_agent_selectors s "
                        "ON s.logical_agent_id=a.logical_agent_id "
                        "WHERE s.selector=? AND s.generation=a.selector_generation "
                        "AND s.retired_at IS NULL AND s.tombstoned_at IS NULL",
                        (selector,),
                    )
                ).fetchone()
                if row is None or row[1] == "deleted":
                    raise PersistentStoreError("slot_not_found")
                logical_agent_id = row[0]
                if int(row[4]) != int(expected_revision):
                    raise PersistentStoreError("revision_conflict")
                if row[2] != authority_node_id:
                    raise PersistentStoreError("authority_unavailable")
                if row[1] == "stopping":
                    raise PersistentStoreError("session_stopping")
                if row[1] == "active":
                    raise PersistentStoreError("session_already_active")
                if row[1] != "armed":
                    raise PersistentStoreError("slot_not_armed")
                arm_row = await (
                    await db.execute(
                        "SELECT generation,armed_at,armed_until,captured_duration_seconds,"
                        "selector_generation,auth_generation,slot_revision,consumed_at,revoked_at "
                        "FROM logical_agent_arms WHERE logical_agent_id=? "
                        "ORDER BY generation DESC LIMIT 1",
                        (logical_agent_id,),
                    )
                ).fetchone()
                if arm_row is None or arm_row[7] is not None or arm_row[8] is not None:
                    raise PersistentStoreError("slot_not_armed")
                if current >= parse_utc(arm_row[2]):
                    await db.execute(
                        "UPDATE logical_agent_arms SET revoked_at=? WHERE logical_agent_id=? "
                        "AND generation=? AND revoked_at IS NULL",
                        (stamp, logical_agent_id, int(arm_row[0])),
                    )
                    await db.execute(
                        "UPDATE logical_agents SET state='suspended',slot_revision=slot_revision+1,"
                        "updated_at=? WHERE logical_agent_id=? AND state='armed'",
                        (stamp, logical_agent_id),
                    )
                    await db.commit()
                    raise PersistentStoreError("arm_expired")
                if int(arm_row[4]) != int(row[5]) or int(arm_row[5]) != int(row[6]):
                    raise PersistentStoreError("policy_incompatible")
                active = await (
                    await db.execute(
                        "SELECT 1 FROM logical_agent_work_sessions WHERE logical_agent_id=? "
                        "AND state IN ('active','stopping') LIMIT 1",
                        (logical_agent_id,),
                    )
                ).fetchone()
                if active is not None:
                    raise PersistentStoreError("session_already_active")
                epoch_row = await (
                    await db.execute(
                        "SELECT COALESCE(MAX(session_epoch),0)+1 FROM logical_agent_work_sessions "
                        "WHERE logical_agent_id=?",
                        (logical_agent_id,),
                    )
                ).fetchone()
                session_epoch = int(epoch_row[0])
                hard_expires_at = utc_text(current + timedelta(seconds=int(arm_row[3])))
                next_revision = int(row[4]) + 1
                await db.execute(
                    "UPDATE logical_agent_arms SET consumed_at=? WHERE logical_agent_id=? "
                    "AND generation=? AND consumed_at IS NULL AND revoked_at IS NULL",
                    (stamp, logical_agent_id, int(arm_row[0])),
                )
                cur = await db.execute(
                    "UPDATE logical_agents SET state='active',slot_revision=?,updated_at=? "
                    "WHERE logical_agent_id=? AND state='armed' AND slot_revision=?",
                    (next_revision, stamp, logical_agent_id, expected_revision),
                )
                if cur.rowcount != 1:
                    raise PersistentStoreError("revision_conflict")
                session = WorkSessionRecord(
                    work_session_id,
                    logical_agent_id,
                    session_epoch,
                    authority_node_id,
                    int(row[3]),
                    stamp,
                    hard_expires_at,
                    principal_id,
                    auth_generation,
                    "active",
                    origin_instance_id=origin_instance_id,
                )
                await db.execute(
                    "INSERT INTO logical_agent_work_sessions("
                    "work_session_id,logical_agent_id,session_epoch,authority_node_id,authority_epoch,"
                    "started_at,hard_expires_at,auth_principal_id,auth_generation,state,"
                    "origin_instance_id,ended_at,end_reason) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                    (
                        session.work_session_id,
                        session.logical_agent_id,
                        session.session_epoch,
                        session.authority_node_id,
                        session.authority_epoch,
                        session.started_at,
                        session.hard_expires_at,
                        session.auth_principal_id,
                        session.auth_generation,
                        session.state,
                        session.origin_instance_id,
                    ),
                )
                await db.commit()
            except PersistentStoreError as exc:
                if exc.code == "arm_expired":
                    raise
                await db.rollback()
                raise
            except Exception:
                await db.rollback()
                raise
        return await self.get_slot(logical_agent_id), session

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

    async def assert_session_authority(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        now: str | None = None,
    ) -> WorkSessionRecord:
        stamp = now or utc_text()
        async with self._connect("persistent_session_authorize") as db:
            row = await (
                await db.execute(
                    "SELECT s.work_session_id,s.logical_agent_id,s.session_epoch,"
                    "s.authority_node_id,"
                    "s.authority_epoch,s.started_at,s.hard_expires_at,s.auth_principal_id,"
                    "s.auth_generation,s.state,s.origin_instance_id,s.ended_at,s.end_reason,"
                    "a.state,a.authority_epoch FROM logical_agent_work_sessions s "
                    "JOIN logical_agents a ON a.logical_agent_id=s.logical_agent_id "
                    "WHERE s.logical_agent_id=? AND s.work_session_id=? AND s.session_epoch=?",
                    (logical_agent_id, work_session_id, session_epoch),
                )
            ).fetchone()
        if row is None:
            raise PersistentStoreError("session_not_found")
        session = self._session(row[:13])
        if session.state != "active" or row[13] != "active":
            raise PersistentStoreError("session_not_active")
        if int(row[14]) != session.authority_epoch:
            raise PersistentStoreError("authority_unavailable")
        if parse_utc(stamp) >= parse_utc(session.hard_expires_at):
            raise PersistentStoreError("session_expired")
        return session

    async def begin_session_stop(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        reason: str,
        now: str | None = None,
    ) -> WorkSessionRecord:
        stamp = now or utc_text()
        async with self._connect("persistent_session_stop_begin") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT state FROM logical_agent_work_sessions WHERE logical_agent_id=? "
                        "AND work_session_id=? AND session_epoch=?",
                        (logical_agent_id, work_session_id, session_epoch),
                    )
                ).fetchone()
                if row is None:
                    raise PersistentStoreError("session_not_found")
                if row[0] not in {"active", "stopping"}:
                    await db.commit()
                    return await self.get_work_session(work_session_id)
                await db.execute(
                    "UPDATE logical_agent_work_sessions SET state='stopping',end_reason=? "
                    "WHERE work_session_id=? AND state='active'",
                    (reason, work_session_id),
                )
                await db.execute(
                    "UPDATE logical_agents SET state='stopping',slot_revision=slot_revision+1,"
                    "updated_at=? WHERE logical_agent_id=? AND state='active'",
                    (stamp, logical_agent_id),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.get_work_session(work_session_id)

    async def finalize_session_stop(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        reason: str,
        terminal_state: str = "ended",
        now: str | None = None,
    ) -> tuple[PersistentSlot, WorkSessionRecord]:
        if terminal_state not in {"ended", "expired", "suspended", "failed"}:
            raise ValueError("invalid terminal session state")
        stamp = now or utc_text()
        async with self._connect("persistent_session_stop_finalize") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "UPDATE logical_agent_work_sessions SET state=?,ended_at=COALESCE(ended_at,?),"
                    "end_reason=? WHERE logical_agent_id=? AND work_session_id=? "
                    "AND session_epoch=? "
                    "AND state IN ('active','stopping')",
                    (
                        terminal_state,
                        stamp,
                        reason,
                        logical_agent_id,
                        work_session_id,
                        session_epoch,
                    ),
                )
                if cur.rowcount == 0:
                    existing = await (
                        await db.execute(
                            "SELECT state FROM logical_agent_work_sessions WHERE work_session_id=?",
                            (work_session_id,),
                        )
                    ).fetchone()
                    if existing is None:
                        raise PersistentStoreError("session_not_found")
                await db.execute(
                    "UPDATE logical_agent_arms SET revoked_at=COALESCE(revoked_at,?) "
                    "WHERE logical_agent_id=? AND consumed_at IS NULL AND revoked_at IS NULL",
                    (stamp, logical_agent_id),
                )
                await db.execute(
                    "UPDATE logical_agents SET state='suspended',slot_revision=slot_revision+1,"
                    "updated_at=? WHERE logical_agent_id=? "
                    "AND state IN ('active','stopping','armed')",
                    (stamp, logical_agent_id),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.get_slot(logical_agent_id), await self.get_work_session(work_session_id)

    async def suspend_non_active(
        self,
        logical_agent_id: str,
        *,
        expected_revision: int,
        now: str | None = None,
    ) -> PersistentSlot:
        stamp = now or utc_text()
        async with self._connect("persistent_slot_suspend") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT state,slot_revision FROM logical_agents WHERE logical_agent_id=?",
                        (logical_agent_id,),
                    )
                ).fetchone()
                if row is None or row[0] == "deleted":
                    raise PersistentStoreError("slot_not_found")
                if int(row[1]) != int(expected_revision):
                    raise PersistentStoreError("revision_conflict")
                if row[0] in {"active", "stopping"}:
                    raise PersistentStoreError(
                        "session_already_active" if row[0] == "active" else "session_stopping"
                    )
                await db.execute(
                    "UPDATE logical_agent_arms SET revoked_at=COALESCE(revoked_at,?) "
                    "WHERE logical_agent_id=? AND consumed_at IS NULL AND revoked_at IS NULL",
                    (stamp, logical_agent_id),
                )
                if row[0] != "suspended":
                    await db.execute(
                        "UPDATE logical_agents SET state='suspended',slot_revision=slot_revision+1,"
                        "updated_at=? WHERE logical_agent_id=?",
                        (stamp, logical_agent_id),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.get_slot(logical_agent_id)

    async def release_logical_claim(
        self, namespace: str, task_id: str, logical_agent_id: str, *, now: str | None = None
    ) -> bool:
        stamp = now or utc_text()
        async with self._connect("persistent_claim_release") as db:
            cur = await db.execute(
                "UPDATE work_claims SET released_at=? WHERE namespace=? AND task_id=? "
                "AND owner_kind='logical_agent' AND owner_id=? AND released_at IS NULL",
                (stamp, namespace, task_id, logical_agent_id),
            )
            await db.commit()
        return cur.rowcount == 1

    async def reassign_logical_claim(
        self,
        namespace: str,
        task_id: str,
        from_logical_agent_id: str,
        to_logical_agent_id: str,
        *,
        now: str | None = None,
    ) -> bool:
        stamp = now or utc_text()
        async with self._connect("persistent_claim_reassign") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                target = await (
                    await db.execute(
                        "SELECT state FROM logical_agents WHERE logical_agent_id=?",
                        (to_logical_agent_id,),
                    )
                ).fetchone()
                if target is None or target[0] == "deleted":
                    raise PersistentStoreError("slot_not_found")
                duplicate = await (
                    await db.execute(
                        "SELECT 1 FROM work_claims WHERE namespace=? AND task_id=? "
                        "AND owner_kind='logical_agent' AND owner_id=? AND released_at IS NULL",
                        (namespace, task_id, to_logical_agent_id),
                    )
                ).fetchone()
                if duplicate is not None:
                    raise PersistentStoreError("reassign_blocked")
                cur = await db.execute(
                    "UPDATE work_claims SET owner_id=?,agent_id=? WHERE namespace=? AND task_id=? "
                    "AND owner_kind='logical_agent' AND owner_id=? AND released_at IS NULL",
                    (
                        to_logical_agent_id,
                        to_logical_agent_id,
                        namespace,
                        task_id,
                        from_logical_agent_id,
                    ),
                )
                if cur.rowcount != 1:
                    raise PersistentStoreError("claim_not_found")
                await db.execute(
                    "INSERT INTO work_events(namespace,task_id,event_type,agent_id,"
                    "payload_json,created_at,"
                    "logical_agent_id) VALUES(?,?,?,?,?,?,?)",
                    (
                        namespace,
                        task_id,
                        "claim_reassigned",
                        to_logical_agent_id,
                        json.dumps(
                            {"from": from_logical_agent_id, "to": to_logical_agent_id},
                            separators=(",", ":"),
                        ),
                        stamp,
                        to_logical_agent_id,
                    ),
                )
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise

    async def delete_slot(
        self,
        logical_agent_id: str,
        *,
        expected_revision: int,
        now: str | None = None,
    ) -> PersistentSlot:
        stamp = now or utc_text()
        async with self._connect("persistent_slot_delete") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT state,slot_revision FROM logical_agents WHERE logical_agent_id=?",
                        (logical_agent_id,),
                    )
                ).fetchone()
                if row is None:
                    raise PersistentStoreError("slot_not_found")
                if row[0] == "deleted":
                    await db.commit()
                    return await self.get_slot(logical_agent_id)
                if int(row[1]) != int(expected_revision):
                    raise PersistentStoreError("revision_conflict")
                if row[0] in {"active", "stopping", "deleting"}:
                    raise PersistentStoreError(
                        "delete_blocked",
                        blockers=[{"kind": row[0], "logical_agent_id": logical_agent_id}],
                    )
                live = await (
                    await db.execute(
                        "SELECT work_session_id,state FROM logical_agent_work_sessions "
                        "WHERE logical_agent_id=? AND state IN ('active','stopping') LIMIT 1",
                        (logical_agent_id,),
                    )
                ).fetchone()
                if live is not None:
                    raise PersistentStoreError(
                        "delete_blocked", blockers=[{"kind": live[1], "work_session_id": live[0]}]
                    )
                await db.execute(
                    "UPDATE logical_agent_arms SET revoked_at=COALESCE(revoked_at,?) "
                    "WHERE logical_agent_id=? AND revoked_at IS NULL",
                    (stamp, logical_agent_id),
                )
                await db.execute(
                    "UPDATE logical_agent_selectors SET retired_at=COALESCE(retired_at,?),"
                    "tombstoned_at=COALESCE(tombstoned_at,?) WHERE logical_agent_id=?",
                    (stamp, stamp, logical_agent_id),
                )
                await db.execute(
                    "UPDATE work_claims SET released_at=? WHERE owner_kind='logical_agent' "
                    "AND owner_id=? AND released_at IS NULL",
                    (stamp, logical_agent_id),
                )
                await db.execute(
                    "UPDATE logical_agents SET state='deleted',deleted_at=?,"
                    "tombstone_reason='operator_delete',"
                    "slot_revision=slot_revision+1,auth_generation=auth_generation+1,updated_at=? "
                    "WHERE logical_agent_id=?",
                    (stamp, stamp, logical_agent_id),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.get_slot(logical_agent_id)

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

    async def fleet_request_reserve(
        self,
        issuer_node_id: str,
        operation: str,
        request_id: str,
        fingerprint: str,
        retain_until: str,
        *,
        now: str | None = None,
    ) -> dict | None:
        stamp = now or utc_text()
        async with self._connect("fleet_request_reserve") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT request_fingerprint,state,result_json FROM fleet_request_dedup "
                        "WHERE issuer_node_id=? AND operation=? AND request_id=?",
                        (issuer_node_id, operation, request_id),
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
                    "INSERT INTO fleet_request_dedup("
                    "issuer_node_id,operation,request_id,request_fingerprint,state,"
                    "result_json,retain_until,created_at) VALUES(?,?,?,?, 'pending',NULL,?,?)",
                    (
                        issuer_node_id,
                        operation,
                        request_id,
                        fingerprint,
                        retain_until,
                        stamp,
                    ),
                )
                await db.commit()
                return None
            except Exception:
                await db.rollback()
                raise

    async def fleet_request_complete(
        self,
        issuer_node_id: str,
        operation: str,
        request_id: str,
        fingerprint: str,
        result: dict,
    ) -> dict:
        payload = json.dumps(result, sort_keys=True, separators=(",", ":"), default=str)
        async with self._connect("fleet_request_complete") as db:
            cur = await db.execute(
                "UPDATE fleet_request_dedup SET state='complete',result_json=? "
                "WHERE issuer_node_id=? AND operation=? AND request_id=? "
                "AND request_fingerprint=? AND state='pending'",
                (payload, issuer_node_id, operation, request_id, fingerprint),
            )
            await db.commit()
        if cur.rowcount != 1:
            async with self._connect("fleet_request_complete_replay") as db:
                row = await (
                    await db.execute(
                        "SELECT request_fingerprint,state,result_json FROM fleet_request_dedup "
                        "WHERE issuer_node_id=? AND operation=? AND request_id=?",
                        (issuer_node_id, operation, request_id),
                    )
                ).fetchone()
            if (
                row is None
                or row[0] != fingerprint
                or row[1] != "complete"
                or row[2] is None
            ):
                raise PersistentStoreError("idempotency_conflict")
            return json.loads(row[2])
        return result

    async def fleet_request_abort(
        self,
        issuer_node_id: str,
        operation: str,
        request_id: str,
        fingerprint: str,
    ) -> bool:
        async with self._connect("fleet_request_abort") as db:
            cur = await db.execute(
                "DELETE FROM fleet_request_dedup "
                "WHERE issuer_node_id=? AND operation=? AND request_id=? "
                "AND request_fingerprint=? AND state='pending'",
                (issuer_node_id, operation, request_id, fingerprint),
            )
            await db.commit()
        return cur.rowcount == 1

    async def fleet_gate(self, logical_agent_id: str) -> dict:
        async with self._connect("fleet_gate_get") as db:
            row = await (
                await db.execute(
                    "SELECT gate_revision,blocked,reason,updated_at "
                    "FROM persistent_fleet_gates WHERE logical_agent_id=?",
                    (logical_agent_id,),
                )
            ).fetchone()
            if row is None:
                slot = await (
                    await db.execute(
                        "SELECT 1 FROM logical_agents WHERE logical_agent_id=?",
                        (logical_agent_id,),
                    )
                ).fetchone()
                if slot is None:
                    raise PersistentStoreError("slot_not_found")
                stamp = utc_text()
                await db.execute(
                    "INSERT INTO persistent_fleet_gates("
                    "logical_agent_id,gate_revision,blocked,reason,updated_at) "
                    "VALUES(?,1,0,NULL,?)",
                    (logical_agent_id, stamp),
                )
                await db.commit()
                return {
                    "logical_agent_id": logical_agent_id,
                    "gate_revision": 1,
                    "blocked": False,
                    "reason": None,
                    "updated_at": stamp,
                }
        return {
            "logical_agent_id": logical_agent_id,
            "gate_revision": int(row[0]),
            "blocked": bool(row[1]),
            "reason": row[2],
            "updated_at": row[3],
        }

    async def fleet_gate_bump(
        self,
        logical_agent_id: str,
        *,
        blocked: bool,
        reason: str | None,
        now: str | None = None,
    ) -> dict:
        stamp = now or utc_text()
        async with self._connect("fleet_gate_bump") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                slot = await (
                    await db.execute(
                        "SELECT 1 FROM logical_agents WHERE logical_agent_id=?",
                        (logical_agent_id,),
                    )
                ).fetchone()
                if slot is None:
                    raise PersistentStoreError("slot_not_found")
                row = await (
                    await db.execute(
                        "SELECT gate_revision FROM persistent_fleet_gates "
                        "WHERE logical_agent_id=?",
                        (logical_agent_id,),
                    )
                ).fetchone()
                revision = (int(row[0]) if row else 0) + 1
                await db.execute(
                    "INSERT INTO persistent_fleet_gates("
                    "logical_agent_id,gate_revision,blocked,reason,updated_at) "
                    "VALUES(?,?,?,?,?) "
                    "ON CONFLICT(logical_agent_id) DO UPDATE SET "
                    "gate_revision=excluded.gate_revision,blocked=excluded.blocked,"
                    "reason=excluded.reason,updated_at=excluded.updated_at",
                    (logical_agent_id, revision, int(blocked), reason, stamp),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "logical_agent_id": logical_agent_id,
            "gate_revision": revision,
            "blocked": bool(blocked),
            "reason": reason,
            "updated_at": stamp,
        }

    async def create_message_obligation(
        self,
        *,
        message_ref: str,
        logical_agent_id: str,
        sender_agent_id: str,
        text: str,
        require_reply: bool,
        alert: bool,
        now: str | None = None,
    ) -> dict:
        stamp = now or utc_text()
        async with self._connect("persistent_message_create") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                existing = await (
                    await db.execute(
                        "SELECT logical_agent_id,sender_agent_id,text,require_reply,alert,"
                        "gate_revision,created_at,resolved_at,resolution "
                        "FROM persistent_message_obligations WHERE message_ref=?",
                        (message_ref,),
                    )
                ).fetchone()
                if existing is not None:
                    await db.commit()
                    return {
                        "message_ref": message_ref,
                        "logical_agent_id": existing[0],
                        "sender_agent_id": existing[1],
                        "text": existing[2],
                        "require_reply": bool(existing[3]),
                        "alert": bool(existing[4]),
                        "gate_revision": int(existing[5]),
                        "created_at": existing[6],
                        "resolved_at": existing[7],
                        "resolution": existing[8],
                        "created": False,
                    }
                slot = await (
                    await db.execute(
                        "SELECT 1 FROM logical_agents WHERE logical_agent_id=?",
                        (logical_agent_id,),
                    )
                ).fetchone()
                if slot is None:
                    raise PersistentStoreError("slot_not_found")
                gate = await (
                    await db.execute(
                        "SELECT gate_revision FROM persistent_fleet_gates "
                        "WHERE logical_agent_id=?",
                        (logical_agent_id,),
                    )
                ).fetchone()
                revision = int(gate[0]) if gate else 1
                gated = bool(require_reply or alert)
                if gated:
                    revision += 1 if gate else 0
                    await db.execute(
                        "INSERT INTO persistent_fleet_gates("
                        "logical_agent_id,gate_revision,blocked,reason,updated_at) "
                        "VALUES(?,?,?,?,?) "
                        "ON CONFLICT(logical_agent_id) DO UPDATE SET "
                        "gate_revision=excluded.gate_revision,blocked=1,"
                        "reason=excluded.reason,updated_at=excluded.updated_at",
                        (
                            logical_agent_id,
                            revision,
                            1,
                            "message_obligation",
                            stamp,
                        ),
                    )
                elif gate is None:
                    await db.execute(
                        "INSERT INTO persistent_fleet_gates("
                        "logical_agent_id,gate_revision,blocked,reason,updated_at) "
                        "VALUES(?,1,0,NULL,?)",
                        (logical_agent_id, stamp),
                    )
                await db.execute(
                    "INSERT INTO persistent_message_obligations("
                    "message_ref,logical_agent_id,sender_agent_id,text,require_reply,alert,"
                    "gate_revision,created_at,resolved_at,resolution) "
                    "VALUES(?,?,?,?,?,?,?,?,NULL,NULL)",
                    (
                        message_ref,
                        logical_agent_id,
                        sender_agent_id,
                        text,
                        int(require_reply),
                        int(alert),
                        revision,
                        stamp,
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "message_ref": message_ref,
            "logical_agent_id": logical_agent_id,
            "sender_agent_id": sender_agent_id,
            "text": text,
            "require_reply": bool(require_reply),
            "alert": bool(alert),
            "gate_revision": revision,
            "created_at": stamp,
            "resolved_at": None,
            "resolution": None,
            "created": True,
        }

    async def open_message_obligations(self, logical_agent_id: str) -> list[dict]:
        async with self._connect("persistent_message_open") as db:
            rows = await (
                await db.execute(
                    "SELECT message_ref,sender_agent_id,text,require_reply,alert,"
                    "gate_revision,created_at FROM persistent_message_obligations "
                    "WHERE logical_agent_id=? AND resolved_at IS NULL "
                    "ORDER BY created_at,message_ref",
                    (logical_agent_id,),
                )
            ).fetchall()
        keys = (
            "message_ref",
            "sender_agent_id",
            "text",
            "require_reply",
            "alert",
            "gate_revision",
            "created_at",
        )
        result = []
        for row in rows:
            item = dict(zip(keys, row, strict=True))
            item["require_reply"] = bool(item["require_reply"])
            item["alert"] = bool(item["alert"])
            item["gate_revision"] = int(item["gate_revision"])
            result.append(item)
        return result

    async def merge_message_receipt(
        self,
        message_ref: str,
        node_instance_id: str,
        *,
        seen_at: str | None = None,
        read_at: str | None = None,
        replied_at: str | None = None,
        reply_message_ref: str | None = None,
    ) -> dict:
        async with self._connect("persistent_message_receipt") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                obligation = await (
                    await db.execute(
                        "SELECT logical_agent_id,require_reply,alert,resolved_at "
                        "FROM persistent_message_obligations WHERE message_ref=?",
                        (message_ref,),
                    )
                ).fetchone()
                if obligation is None:
                    raise PersistentStoreError("message_not_found")
                row = await (
                    await db.execute(
                        "SELECT seen_at,read_at,replied_at,reply_message_ref "
                        "FROM persistent_message_receipts "
                        "WHERE message_ref=? AND node_instance_id=?",
                        (message_ref, node_instance_id),
                    )
                ).fetchone()

                def earliest(current, incoming):
                    if current is None:
                        return incoming
                    if incoming is None:
                        return current
                    return min(current, incoming)

                current = row or (None, None, None, None)
                merged = (
                    earliest(current[0], seen_at),
                    earliest(current[1], read_at),
                    earliest(current[2], replied_at),
                    current[3] or reply_message_ref,
                )
                await db.execute(
                    "INSERT INTO persistent_message_receipts("
                    "message_ref,node_instance_id,seen_at,read_at,replied_at,reply_message_ref) "
                    "VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(message_ref,node_instance_id) DO UPDATE SET "
                    "seen_at=excluded.seen_at,read_at=excluded.read_at,"
                    "replied_at=excluded.replied_at,reply_message_ref=excluded.reply_message_ref",
                    (message_ref, node_instance_id, *merged),
                )
                should_resolve = bool(replied_at) and bool(obligation[1] or obligation[2])
                if should_resolve and obligation[3] is None:
                    await db.execute(
                        "UPDATE persistent_message_obligations "
                        "SET resolved_at=?,resolution='reply' "
                        "WHERE message_ref=? AND resolved_at IS NULL",
                        (replied_at, message_ref),
                    )
                    remaining = await (
                        await db.execute(
                            "SELECT COUNT(*) FROM persistent_message_obligations "
                            "WHERE logical_agent_id=? AND resolved_at IS NULL "
                            "AND (require_reply=1 OR alert=1)",
                            (obligation[0],),
                        )
                    ).fetchone()
                    gate = await (
                        await db.execute(
                            "SELECT gate_revision FROM persistent_fleet_gates "
                            "WHERE logical_agent_id=?",
                            (obligation[0],),
                        )
                    ).fetchone()
                    revision = (int(gate[0]) if gate else 1) + 1
                    blocked = int(int(remaining[0]) > 0)
                    await db.execute(
                        "INSERT INTO persistent_fleet_gates("
                        "logical_agent_id,gate_revision,blocked,reason,updated_at) "
                        "VALUES(?,?,?,?,?) "
                        "ON CONFLICT(logical_agent_id) DO UPDATE SET "
                        "gate_revision=excluded.gate_revision,blocked=excluded.blocked,"
                        "reason=excluded.reason,updated_at=excluded.updated_at",
                        (
                            obligation[0],
                            revision,
                            blocked,
                            "message_obligation" if blocked else None,
                            replied_at,
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "message_ref": message_ref,
            "node_instance_id": node_instance_id,
            "seen_at": merged[0],
            "read_at": merged[1],
            "replied_at": merged[2],
            "reply_message_ref": merged[3],
        }

    async def record_node_attachment(
        self,
        *,
        node_attachment_id: str,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        node_instance_id: str,
        authority_epoch: int,
        hard_expires_at: str,
        now: str | None = None,
    ) -> dict:
        stamp = now or utc_text()
        async with self._connect("persistent_attachment_record") as db:
            await db.execute(
                "INSERT INTO logical_agent_node_attachments("
                "node_attachment_id,logical_agent_id,work_session_id,session_epoch,node_instance_id,"
                "authority_epoch,attached_at,hard_expires_at,revoked_at) "
                "VALUES(?,?,?,?,?,?,?,?,NULL) "
                "ON CONFLICT(logical_agent_id,work_session_id,session_epoch,node_instance_id) "
                "DO UPDATE SET authority_epoch=excluded.authority_epoch,"
                "hard_expires_at=excluded.hard_expires_at,revoked_at=NULL",
                (
                    node_attachment_id,
                    logical_agent_id,
                    work_session_id,
                    session_epoch,
                    node_instance_id,
                    authority_epoch,
                    stamp,
                    hard_expires_at,
                ),
            )
            await db.commit()
            row = await (
                await db.execute(
                    "SELECT node_attachment_id,logical_agent_id,work_session_id,session_epoch,"
                    "node_instance_id,authority_epoch,attached_at,hard_expires_at,revoked_at "
                    "FROM logical_agent_node_attachments WHERE logical_agent_id=? "
                    "AND work_session_id=? "
                    "AND session_epoch=? AND node_instance_id=?",
                    (logical_agent_id, work_session_id, session_epoch, node_instance_id),
                )
            ).fetchone()
        keys = (
            "node_attachment_id",
            "logical_agent_id",
            "work_session_id",
            "session_epoch",
            "node_instance_id",
            "authority_epoch",
            "attached_at",
            "hard_expires_at",
            "revoked_at",
        )
        return dict(zip(keys, row, strict=True))

    async def attachments_for_session(
        self, logical_agent_id: str, work_session_id: str, session_epoch: int, *, active_only=True
    ) -> list[dict]:
        clause = " AND revoked_at IS NULL" if active_only else ""
        async with self._connect("persistent_attachment_list") as db:
            rows = await (
                await db.execute(
                    "SELECT node_attachment_id,logical_agent_id,work_session_id,session_epoch,"
                    "node_instance_id,authority_epoch,attached_at,hard_expires_at,revoked_at "
                    "FROM logical_agent_node_attachments WHERE logical_agent_id=? "
                    "AND work_session_id=? "
                    "AND session_epoch=?" + clause + " ORDER BY attached_at,node_instance_id",
                    (logical_agent_id, work_session_id, session_epoch),
                )
            ).fetchall()
        keys = (
            "node_attachment_id",
            "logical_agent_id",
            "work_session_id",
            "session_epoch",
            "node_instance_id",
            "authority_epoch",
            "attached_at",
            "hard_expires_at",
            "revoked_at",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    async def revoke_node_attachment(
        self, node_attachment_id: str, *, now: str | None = None
    ) -> bool:
        stamp = now or utc_text()
        async with self._connect("persistent_attachment_revoke") as db:
            cur = await db.execute(
                "UPDATE logical_agent_node_attachments SET revoked_at=COALESCE(revoked_at,?) "
                "WHERE node_attachment_id=?",
                (stamp, node_attachment_id),
            )
            await db.commit()
        return cur.rowcount > 0

    async def record_command_permit(self, command_hash: str, permit: dict) -> None:
        async with self._connect("persistent_permit_record") as db:
            await db.execute(
                "INSERT OR REPLACE INTO persistent_command_permits("
                "command_hash,logical_agent_id,work_session_id,session_epoch,authority_node_id,"
                "authority_epoch,node_attachment_id,node_instance_id,scope,hard_expires_at,permit_expires_at,"
                "signature,created_at,revoked_at,gate_revision,operation,ttl_ms,slot_revision,principal_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?)",
                (
                    command_hash,
                    permit["logical_agent_id"],
                    permit["work_session_id"],
                    int(permit["session_epoch"]),
                    permit["authority_node_id"],
                    int(permit["authority_epoch"]),
                    permit["node_attachment_id"],
                    permit["node_instance_id"],
                    permit["scope"],
                    permit["hard_expires_at"],
                    permit["permit_expires_at"],
                    permit["signature"],
                    permit["issued_at"],
                    int(permit.get("gate_revision", 1)),
                    str(permit.get("operation") or permit["scope"]),
                    int(permit.get("ttl_ms", 10000)),
                    int(permit["slot_revision"]) if permit.get("slot_revision") is not None else None,
                    permit.get("principal_id"),
                ),
            )
            await db.commit()

    async def revoke_command_permits(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        now: str | None = None,
    ) -> int:
        stamp = now or utc_text()
        async with self._connect("persistent_permit_revoke") as db:
            cur = await db.execute(
                "UPDATE persistent_command_permits SET revoked_at=COALESCE(revoked_at,?) "
                "WHERE logical_agent_id=? AND work_session_id=? AND session_epoch=? "
                "AND revoked_at IS NULL",
                (stamp, logical_agent_id, work_session_id, session_epoch),
            )
            await db.commit()
        return int(cur.rowcount)

    async def expired_remote_permit_sessions(self, *, now: str | None = None) -> list[dict]:
        stamp = now or utc_text()
        async with self._connect("persistent_permit_expired") as db:
            rows = await (
                await db.execute(
                    "SELECT DISTINCT logical_agent_id,work_session_id,session_epoch,"
                    "authority_node_id "
                    "FROM persistent_command_permits WHERE revoked_at IS NULL "
                    "AND hard_expires_at<=?",
                    (stamp,),
                )
            ).fetchall()
        return [
            {
                "logical_agent_id": row[0],
                "work_session_id": row[1],
                "session_epoch": int(row[2]),
                "authority_node_id": row[3],
            }
            for row in rows
        ]

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
