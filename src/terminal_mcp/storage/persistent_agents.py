from __future__ import annotations

from contextlib import asynccontextmanager

import aiosqlite

from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.core.persistent_agents import (
    ArmGeneration,
    PersistentSlot,
    WorkSessionRecord,
    normalize_slot_selector,
)
from terminal_mcp.storage.sqlite_observability import SqliteDiagnostics, observed_connection


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
