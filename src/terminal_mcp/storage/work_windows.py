"""Authoritative, additive persistence for managed work-window budgets.

The existing logical_agents and logical_agent_work_sessions rows stay canonical.
New tables attach policy, provider bindings and immutable session provenance; no
parallel identity/session universe is created. This store is deliberately not
wired to legacy transports: application admission, Mesh routing and the actual
ExecutionFence must be supplied before enabling the managed surface.
"""

from __future__ import annotations

import json
import math
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta

import aiosqlite

from terminal_mcp.core.managed_sessions import (
    ManagedSessionError,
    ManagedSessionSnapshot,
    SlotPolicyRecord,
)
from terminal_mcp.core.orchestration import parse_utc, utc_now, utc_text
from terminal_mcp.core.persistent_agents import WorkSessionRecord
from terminal_mcp.core.provider_identity import ProviderIdentity
from terminal_mcp.core.window_recovery import (
    MAX_RECOVERY_BATCH,
    MAX_RECOVERY_LEASE_SECONDS,
    RECOVERY_ERRORS,
    WindowRecoveryLease,
)
from terminal_mcp.core.work_windows import (
    SlotSessionPolicy,
    WindowChange,
    WindowLifecycle,
    WindowPhase,
    WorkSessionBinding,
    WorkWindow,
)
from terminal_mcp.storage.persistent_agents import PersistentAgentStore
from terminal_mcp.storage.window_recovery import (
    install_window_recovery_schema,
    schedule_window_recovery,
)

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS logical_agent_provider_bindings(
        provider TEXT NOT NULL, binding_key TEXT NOT NULL,
        logical_agent_id TEXT NOT NULL REFERENCES logical_agents(logical_agent_id),
        created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
        PRIMARY KEY(provider,binding_key))""",
    """CREATE TABLE IF NOT EXISTS logical_agent_session_policies(
        logical_agent_id TEXT PRIMARY KEY REFERENCES logical_agents(logical_agent_id),
        revision INTEGER NOT NULL CHECK(revision > 0),
        policy_json TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS logical_agent_work_windows(
        work_window_id TEXT PRIMARY KEY,
        logical_agent_id TEXT NOT NULL REFERENCES logical_agents(logical_agent_id),
        window_revision INTEGER NOT NULL CHECK(window_revision > 0),
        snapshot_json TEXT NOT NULL, superseded_at TEXT)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_logical_agent_current_window
        ON logical_agent_work_windows(logical_agent_id) WHERE superseded_at IS NULL""",
    """CREATE TABLE IF NOT EXISTS logical_agent_session_bindings(
        work_session_id TEXT PRIMARY KEY
            REFERENCES logical_agent_work_sessions(work_session_id),
        logical_agent_id TEXT NOT NULL REFERENCES logical_agents(logical_agent_id),
        work_window_id TEXT NOT NULL REFERENCES logical_agent_work_windows(work_window_id),
        session_epoch INTEGER NOT NULL CHECK(session_epoch > 0),
        role TEXT NOT NULL CHECK(role IN ('legacy','executor','coordinator')),
        contract_version INTEGER NOT NULL CHECK(contract_version > 0))""",
    """CREATE INDEX IF NOT EXISTS ix_logical_agent_session_bindings_window
        ON logical_agent_session_bindings(work_window_id,session_epoch)""",
)
_SESSION_COLUMNS = (
    "work_session_id,logical_agent_id,session_epoch,authority_node_id,authority_epoch,"
    "started_at,hard_expires_at,auth_principal_id,auth_generation,state,"
    "origin_instance_id,ended_at,end_reason"
)
_BINDING_COLUMNS = (
    "work_session_id,logical_agent_id,work_window_id,session_epoch,role,contract_version"
)


async def install_work_window_schema(db: aiosqlite.Connection) -> None:
    # execute(), not executescript(): do not commit the caller's migration early.
    for sql in _SCHEMA:
        await db.execute(sql)
    await install_window_recovery_schema(db)


class WorkWindowStoreError(ManagedSessionError):
    pass


def _revision(value: int) -> int:
    if type(value) is not int or value < 1:
        raise WorkWindowStoreError("revision_invalid")
    return value


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise WorkWindowStoreError("identity_invalid")
    if any(ord(c) < 32 for c in value):
        raise WorkWindowStoreError("identity_invalid")
    return value


def _dump(window: WorkWindow) -> str:
    values = asdict(window)
    values["opened_at"] = utc_text(window.opened_at)
    values["expired_at"] = utc_text(window.expired_at) if window.expired_at else None
    return json.dumps(values, separators=(",", ":"), sort_keys=True)


def _load(value: str) -> WorkWindow:
    data = json.loads(value)
    data["opened_at"] = parse_utc(data["opened_at"])
    if data["expired_at"] is not None:
        data["expired_at"] = parse_utc(data["expired_at"])
    data["lifecycle"] = WindowLifecycle(data["lifecycle"])
    return WorkWindow(**data)


class WorkWindowStore(PersistentAgentStore):
    def __init__(self, path, *, authority_node_id: str, defaults: SlotSessionPolicy | None = None):
        super().__init__(path)
        self.authority_node_id = _identifier(authority_node_id)
        if defaults is not None and not isinstance(defaults, SlotSessionPolicy):
            raise WorkWindowStoreError("policy_invalid")
        self.defaults = defaults or SlotSessionPolicy()

    @asynccontextmanager
    async def _transaction(self, operation: str):
        async with self._connect(operation) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def _home(self, db, agent: str):
        _identifier(agent)
        row = await (
            await db.execute(
                "SELECT state,authority_node_id,authority_epoch,auth_generation "
                "FROM logical_agents WHERE logical_agent_id=?",
                (agent,),
            )
        ).fetchone()
        if row is None or row[0] == "deleted":
            raise WorkWindowStoreError("slot_not_found")
        if row[1] != self.authority_node_id:
            raise WorkWindowStoreError("authority_unavailable")
        return row

    @staticmethod
    async def _current(db, agent: str) -> WorkWindow | None:
        row = await (
            await db.execute(
                "SELECT snapshot_json FROM logical_agent_work_windows "
                "WHERE logical_agent_id=? AND superseded_at IS NULL",
                (agent,),
            )
        ).fetchone()
        return _load(row[0]) if row else None

    @staticmethod
    async def _session_on(db, session_id: str) -> WorkSessionRecord | None:
        row = await (
            await db.execute(
                f"SELECT {_SESSION_COLUMNS} FROM logical_agent_work_sessions "
                "WHERE work_session_id=?",
                (session_id,),
            )
        ).fetchone()
        return WorkSessionRecord(*row) if row else None

    @staticmethod
    async def _binding_on(db, session_id: str) -> WorkSessionBinding | None:
        row = await (
            await db.execute(
                f"SELECT {_BINDING_COLUMNS} FROM logical_agent_session_bindings "
                "WHERE work_session_id=?",
                (session_id,),
            )
        ).fetchone()
        return WorkSessionBinding(*row) if row else None

    async def _exact_session(self, db, agent, session_id, epoch):
        _revision(epoch)
        session = await self._session_on(db, session_id)
        if session is None or session.logical_agent_id != agent or session.session_epoch != epoch:
            raise WorkWindowStoreError("session_not_found")
        binding = await self._binding_on(db, session_id)
        if binding is None:
            raise WorkWindowStoreError("session_migration_required")
        if binding.logical_agent_id != agent or binding.session_epoch != epoch:
            raise WorkWindowStoreError("session_binding_invalid")
        return session, binding

    async def _policy_on(self, db, agent, stamp) -> SlotPolicyRecord:
        await db.execute(
            "INSERT OR IGNORE INTO logical_agent_session_policies VALUES(?,1,?,?)",
            (agent, json.dumps(asdict(self.defaults), separators=(",", ":")), stamp),
        )
        row = await (
            await db.execute(
                "SELECT policy_json,revision FROM logical_agent_session_policies "
                "WHERE logical_agent_id=?",
                (agent,),
            )
        ).fetchone()
        return SlotPolicyRecord(SlotSessionPolicy(**json.loads(row[0])), row[1])

    @staticmethod
    async def _audit(db, agent, event, principal, stamp, payload, session=None):
        _identifier(principal)
        await db.execute(
            "INSERT INTO persistent_agent_audit(logical_agent_id,event_type,principal_id,"
            "work_session_id,session_epoch,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                agent,
                event,
                principal,
                session.work_session_id if session else None,
                session.session_epoch if session else None,
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                stamp,
            ),
        )

    @staticmethod
    async def _insert_window(db, window):
        await db.execute(
            "INSERT INTO logical_agent_work_windows VALUES(?,?,?,?,NULL)",
            (window.work_window_id, window.logical_agent_id, window.window_revision, _dump(window)),
        )
        await schedule_window_recovery(db, window)

    @staticmethod
    async def _replace_window(db, previous, current):
        cur = await db.execute(
            "UPDATE logical_agent_work_windows SET snapshot_json=?,window_revision=? "
            "WHERE work_window_id=? AND window_revision=? AND superseded_at IS NULL",
            (
                _dump(current),
                current.window_revision,
                previous.work_window_id,
                previous.window_revision,
            ),
        )
        if cur.rowcount != 1:
            raise WorkWindowStoreError("revision_conflict")
        await schedule_window_recovery(db, current)

    @staticmethod
    async def _insert_binding(db, binding):
        await db.execute(
            "INSERT INTO logical_agent_session_bindings VALUES(?,?,?,?,?,?)",
            tuple(asdict(binding).values()),
        )

    async def bind_provider(
        self,
        identity: ProviderIdentity,
        logical_agent_id: str,
        *,
        principal_id: str,
        now: datetime | None = None,
    ) -> str:
        """Attach evidence to an already authorized/provisioned slot, never authorize it."""
        stamp = utc_text(now or utc_now())
        async with self._transaction("provider_binding") as db:
            await self._home(db, logical_agent_id)
            row = await (
                await db.execute(
                    "SELECT logical_agent_id FROM logical_agent_provider_bindings "
                    "WHERE provider=? AND binding_key=?",
                    (identity.provider, identity.binding_key),
                )
            ).fetchone()
            if row and row[0] != logical_agent_id:
                raise WorkWindowStoreError("identity_binding_conflict")
            if row is None:
                await db.execute(
                    "INSERT INTO logical_agent_provider_bindings VALUES(?,?,?,?,?)",
                    (identity.provider, identity.binding_key, logical_agent_id, stamp, stamp),
                )
                await self._audit(
                    db,
                    logical_agent_id,
                    "provider_bound",
                    principal_id,
                    stamp,
                    identity.diagnostic(),
                )
            else:
                await db.execute(
                    "UPDATE logical_agent_provider_bindings SET last_seen_at=? "
                    "WHERE provider=? AND binding_key=?",
                    (stamp, identity.provider, identity.binding_key),
                )
        return logical_agent_id

    async def resolve_provider(self, identity: ProviderIdentity) -> str | None:
        # Identity resolution is fleet-wide evidence lookup, not local authority
        # admission. A non-authority node must be able to learn the LogicalAgent
        # and route the call to its current authority. Binding remains authority-only.
        async with self._connect("provider_resolve") as db:
            row = await (
                await db.execute(
                    "SELECT b.logical_agent_id,a.state FROM logical_agent_provider_bindings b "
                    "JOIN logical_agents a ON a.logical_agent_id=b.logical_agent_id "
                    "WHERE b.provider=? AND b.binding_key=?",
                    (identity.provider, identity.binding_key),
                )
            ).fetchone()
        return row[0] if row and row[1] != "deleted" else None

    async def policy(self, logical_agent_id: str) -> SlotPolicyRecord:
        async with self._transaction("window_policy_read") as db:
            await self._home(db, logical_agent_id)
            return await self._policy_on(db, logical_agent_id, utc_text())

    async def update_policy(
        self,
        logical_agent_id: str,
        policy: SlotSessionPolicy,
        *,
        expected_revision: int,
        principal_id: str,
        now: datetime | None = None,
    ) -> SlotPolicyRecord:
        _revision(expected_revision)
        if not isinstance(policy, SlotSessionPolicy):
            raise WorkWindowStoreError("policy_invalid")
        stamp = utc_text(now or utc_now())
        async with self._transaction("window_policy_update") as db:
            await self._home(db, logical_agent_id)
            old = await self._policy_on(db, logical_agent_id, stamp)
            if old.revision != expected_revision:
                raise WorkWindowStoreError("revision_conflict", current=old)
            await db.execute(
                "UPDATE logical_agent_session_policies SET revision=revision+1,"
                "policy_json=?,updated_at=? WHERE logical_agent_id=? AND revision=?",
                (json.dumps(asdict(policy)), stamp, logical_agent_id, expected_revision),
            )
            await self._audit(
                db,
                logical_agent_id,
                "slot_policy_changed",
                principal_id,
                stamp,
                {"previous": asdict(old), "current": asdict(policy)},
            )
        return SlotPolicyRecord(policy, old.revision + 1)

    async def current_window(self, logical_agent_id: str) -> WorkWindow | None:
        async with self._connect("work_window_read") as db:
            await self._home(db, logical_agent_id)
            return await self._current(db, logical_agent_id)

    async def session_snapshot(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        require_live: bool = False,
    ) -> ManagedSessionSnapshot:
        async with self._connect("managed_session_read") as db:
            await db.execute("BEGIN")
            slot = await self._home(db, logical_agent_id)
            session, binding = await self._exact_session(
                db, logical_agent_id, work_session_id, session_epoch
            )
            row = await (
                await db.execute(
                    "SELECT snapshot_json,superseded_at FROM logical_agent_work_windows "
                    "WHERE work_window_id=?",
                    (binding.work_window_id,),
                )
            ).fetchone()
            if row is None:
                raise WorkWindowStoreError("session_binding_invalid")
            window = _load(row[0])
            if window.logical_agent_id != logical_agent_id:
                raise WorkWindowStoreError("session_binding_invalid")
            if require_live:
                if window.lifecycle in {WindowLifecycle.EXPIRED, WindowLifecycle.COOLDOWN}:
                    raise WorkWindowStoreError(
                        "session_expired", current=window, return_to_chat=True
                    )
                if session.state != "active" or slot[0] != "active":
                    raise WorkWindowStoreError("session_not_active", return_to_chat=True)
                if (
                    session.authority_node_id != self.authority_node_id
                    or session.authority_epoch != slot[2]
                ):
                    raise WorkWindowStoreError("session_authority_stale", return_to_chat=True)
                if session.auth_generation != slot[3]:
                    raise WorkWindowStoreError("auth_generation_mismatch", return_to_chat=True)
                if row[1] is not None or session.hard_expires_at != utc_text(
                    window.hard_expires_at
                ):
                    raise WorkWindowStoreError("session_binding_invalid", return_to_chat=True)
            return ManagedSessionSnapshot(window, session, binding)

    async def start_managed_session(
        self,
        logical_agent_id: str,
        *,
        role: str,
        contract_version: int,
        principal_id: str,
        auth_generation: int,
        origin_instance_id: str | None = None,
        now: datetime | None = None,
    ) -> ManagedSessionSnapshot:
        _identifier(principal_id)
        _revision(auth_generation)
        now = now or utc_now()
        stamp = utc_text(now)
        # Validate role/version even on the idempotent or rejected start paths.
        WorkSessionBinding("validate", logical_agent_id, "validate", 1, role, contract_version)
        async with self._transaction("managed_session_start") as db:
            slot = await self._home(db, logical_agent_id)
            if slot[3] != auth_generation:
                raise WorkWindowStoreError("auth_generation_mismatch")
            if slot[0] not in {"armed", "active"}:
                raise WorkWindowStoreError(
                    "session_stopping" if slot[0] == "stopping" else "slot_not_armed"
                )
            window = await self._current(db, logical_agent_id)
            live = await (
                await db.execute(
                    f"SELECT {_SESSION_COLUMNS} FROM logical_agent_work_sessions "
                    "WHERE logical_agent_id=? AND state IN ('active','stopping') "
                    "ORDER BY session_epoch DESC LIMIT 2",
                    (logical_agent_id,),
                )
            ).fetchall()
            if len(live) > 1:
                raise WorkWindowStoreError("session_state_invalid")
            if live:
                session = WorkSessionRecord(*live[0])
                binding = await self._binding_on(db, session.work_session_id)
                if binding is None or window is None:
                    raise WorkWindowStoreError("session_migration_required")
                if (
                    binding.logical_agent_id != logical_agent_id
                    or binding.session_epoch != session.session_epoch
                    or binding.work_window_id != window.work_window_id
                    or session.authority_node_id != self.authority_node_id
                    or session.authority_epoch != slot[2]
                ):
                    raise WorkWindowStoreError("session_binding_invalid")
                if session.state != "active":
                    raise WorkWindowStoreError("session_stopping")
                if (
                    session.auth_principal_id != principal_id
                    or session.auth_generation != auth_generation
                ):
                    raise WorkWindowStoreError("session_principal_mismatch")
                binding.require_contract(role, contract_version)
                if window.phase(now) in {WindowPhase.EXPIRED, WindowPhase.COOLDOWN}:
                    raise WorkWindowStoreError("session_expired", current=window)
                return ManagedSessionSnapshot(window, session, binding)
            if slot[0] == "active":
                raise WorkWindowStoreError("session_state_invalid")
            if window and window.lifecycle is WindowLifecycle.COOLDOWN:
                if not window.successor_allowed(now):
                    raise WorkWindowStoreError("window_cooldown", current=window)
                await db.execute(
                    "UPDATE logical_agent_work_windows SET superseded_at=? "
                    "WHERE work_window_id=? AND superseded_at IS NULL",
                    (stamp, window.work_window_id),
                )
                window = None
            if window and window.phase(now) is WindowPhase.EXPIRED:
                raise WorkWindowStoreError("session_expired", current=window)
            if window is None:
                policy = await self._policy_on(db, logical_agent_id, stamp)
                window = WorkWindow.open(
                    "ww_" + secrets.token_urlsafe(18), logical_agent_id, now, policy.policy
                )
                await self._insert_window(db, window)
                await self._audit(
                    db,
                    logical_agent_id,
                    "work_window_opened",
                    principal_id,
                    stamp,
                    json.loads(_dump(window)),
                )
            row = await (
                await db.execute(
                    "SELECT COALESCE(MAX(session_epoch),0)+1 FROM logical_agent_work_sessions "
                    "WHERE logical_agent_id=?",
                    (logical_agent_id,),
                )
            ).fetchone()
            session = WorkSessionRecord(
                "ws_" + secrets.token_urlsafe(18),
                logical_agent_id,
                row[0],
                self.authority_node_id,
                slot[2],
                stamp,
                utc_text(window.hard_expires_at),
                principal_id,
                auth_generation,
                "active",
                origin_instance_id,
            )
            binding = WorkSessionBinding(
                session.work_session_id,
                logical_agent_id,
                window.work_window_id,
                session.session_epoch,
                role,
                contract_version,
            )
            await db.execute(
                f"INSERT INTO logical_agent_work_sessions({_SESSION_COLUMNS}) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(asdict(session).values()),
            )
            await self._insert_binding(db, binding)
            await db.execute(
                "UPDATE logical_agents SET state='active',slot_revision=slot_revision+1,"
                "updated_at=? WHERE logical_agent_id=?",
                (stamp, logical_agent_id),
            )
            await self._audit(
                db,
                logical_agent_id,
                "managed_session_started",
                principal_id,
                stamp,
                asdict(binding),
                session,
            )
            return ManagedSessionSnapshot(window, session, binding, created=True)

    async def _revoke_on(self, db, window, stamp):
        await db.execute(
            "UPDATE logical_agent_work_sessions SET state='stopping',end_reason=? "
            "WHERE state='active' AND work_session_id IN ("
            "SELECT work_session_id FROM logical_agent_session_bindings WHERE work_window_id=?)",
            (window.expiry_reason or "hard_duration", window.work_window_id),
        )
        await db.execute(
            "UPDATE logical_agents SET state='stopping',slot_revision=slot_revision+1,updated_at=? "
            "WHERE logical_agent_id=? AND state='active'",
            (stamp, window.logical_agent_id),
        )

    async def change_window(
        self,
        logical_agent_id: str,
        *,
        expected_revision: int,
        principal_id: str,
        now: datetime | None = None,
        duration_seconds: int | None = None,
        delta_seconds: int | None = None,
        warning_before_expiry_seconds: int | None = None,
        draining_before_expiry_seconds: int | None = None,
    ) -> WindowChange:
        _revision(expected_revision)
        now = now or utc_now()
        stamp = utc_text(now)
        async with self._transaction("work_window_change") as db:
            await self._home(db, logical_agent_id)
            old = await self._current(db, logical_agent_id)
            if old is None:
                raise WorkWindowStoreError("window_not_found")
            if old.window_revision != expected_revision:
                raise WorkWindowStoreError("revision_conflict", current=old)
            change = old.change(
                now=now,
                expected_revision=expected_revision,
                duration_seconds=duration_seconds,
                delta_seconds=delta_seconds,
                warning_before_expiry_seconds=warning_before_expiry_seconds,
                draining_before_expiry_seconds=draining_before_expiry_seconds,
            )
            await self._replace_window(db, old, change.current)
            # The existing execution gate reads this exact canonical row, so a
            # deadline update takes effect before an OS cancellation can begin.
            await db.execute(
                "UPDATE logical_agent_work_sessions SET hard_expires_at=? "
                "WHERE state IN ('active','stopping') AND work_session_id IN ("
                "SELECT work_session_id FROM logical_agent_session_bindings "
                "WHERE work_window_id=?)",
                (utc_text(change.current.hard_expires_at), old.work_window_id),
            )
            if change.state is WindowPhase.EXPIRED:
                await self._revoke_on(db, change.current, stamp)
            await self._audit(
                db,
                logical_agent_id,
                "work_window_changed",
                principal_id,
                stamp,
                {"previous": json.loads(_dump(old)), "current": json.loads(_dump(change.current))},
            )
            return change

    async def expire_window(
        self, logical_agent_id: str, *, expected_revision: int, now: datetime | None = None
    ) -> WorkWindow:
        _revision(expected_revision)
        now = now or utc_now()
        async with self._transaction("work_window_expire") as db:
            await self._home(db, logical_agent_id)
            old = await self._current(db, logical_agent_id)
            if old is None:
                raise WorkWindowStoreError("window_not_found")
            current = old.expire(now=now, expected_revision=expected_revision)
            if current != old:
                await self._replace_window(db, old, current)
                await self._revoke_on(db, current, utc_text(now))
                await self._audit(
                    db,
                    logical_agent_id,
                    "work_window_expired",
                    "system",
                    utc_text(now),
                    json.loads(_dump(current)),
                )
            return current

    async def begin_managed_stop(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        principal_id: str,
        reason: str = "session_end",
        now: datetime | None = None,
    ):
        _identifier(reason)
        stamp = utc_text(now or utc_now())
        async with self._transaction("managed_session_stop") as db:
            await self._home(db, logical_agent_id)
            session, _ = await self._exact_session(
                db, logical_agent_id, work_session_id, session_epoch
            )
            if session.state != "active":
                return session
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
            await self._audit(
                db,
                logical_agent_id,
                "managed_session_stopping",
                principal_id,
                stamp,
                {"reason": reason},
                session,
            )
            window = await self._current(db, logical_agent_id)
            if window is not None:
                await schedule_window_recovery(db, window, now=now, due_at=now or utc_now())
            return replace(session, state="stopping", end_reason=reason)

    @staticmethod
    async def _command_blocked(db, agent, session_id=None):
        condition = " AND a.work_session_id=?" if session_id is not None else ""
        params = (agent, session_id) if session_id is not None else (agent,)
        return (
            await (
                await db.execute(
                    "SELECT 1 FROM commands c JOIN command_agent_attribution a "
                    "ON a.command_hash=c.hash WHERE a.logical_agent_id=? "
                    "AND c.status IN ('queued','running')" + condition + " LIMIT 1",
                    params,
                )
            ).fetchone()
            is not None
        )

    async def finish_managed_stop(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        principal_id: str,
        now: datetime | None = None,
    ) -> WorkSessionRecord:
        """Application MUST first complete ExecutionFence; durable blockers double-check it."""
        now = now or utc_now()
        stamp = utc_text(now)
        async with self._transaction("managed_session_finalize") as db:
            await self._home(db, logical_agent_id)
            session, binding = await self._exact_session(
                db, logical_agent_id, work_session_id, session_epoch
            )
            if session.state not in {"active", "stopping"}:
                return session  # Late retry must not touch a newer active session/slot.
            if session.state != "stopping":
                raise WorkWindowStoreError("session_not_stopping")
            if await self._command_blocked(db, logical_agent_id, work_session_id):
                raise WorkWindowStoreError("session_drain_pending")
            window = await self._current(db, logical_agent_id)
            if window is None or window.work_window_id != binding.work_window_id:
                raise WorkWindowStoreError("session_binding_invalid")
            terminal = "expired" if window.phase(now) is WindowPhase.EXPIRED else "ended"
            result = replace(session, state=terminal, ended_at=stamp)
            await db.execute(
                "UPDATE logical_agent_work_sessions SET state=?,ended_at=? WHERE work_session_id=?",
                (terminal, stamp, work_session_id),
            )
            await db.execute(
                "UPDATE logical_agents SET state='armed',slot_revision=slot_revision+1,"
                "updated_at=? "
                "WHERE logical_agent_id=? AND state='stopping'",
                (stamp, logical_agent_id),
            )
            await self._audit(
                db,
                logical_agent_id,
                "managed_session_ended",
                principal_id,
                stamp,
                {"state": terminal, "reason": result.end_reason},
                result,
            )
            return result

    async def complete_window_fence(
        self, logical_agent_id: str, *, expected_revision: int, now: datetime | None = None
    ) -> WorkWindow:
        """Persist cooldown only after authoritative revocation AND execution drainage."""
        _revision(expected_revision)
        now = now or utc_now()
        async with self._transaction("work_window_fence_complete") as db:
            await self._home(db, logical_agent_id)
            old = await self._current(db, logical_agent_id)
            if old is None:
                raise WorkWindowStoreError("window_not_found")
            current = old.begin_cooldown(expected_revision=expected_revision)
            live = await (
                await db.execute(
                    "SELECT 1 FROM logical_agent_work_sessions WHERE logical_agent_id=? "
                    "AND state IN ('active','stopping') LIMIT 1",
                    (logical_agent_id,),
                )
            ).fetchone()
            if live or await self._command_blocked(db, logical_agent_id):
                raise WorkWindowStoreError("session_drain_pending")
            if current != old:
                await self._replace_window(db, old, current)
                await self._audit(
                    db,
                    logical_agent_id,
                    "work_window_cooldown",
                    "system",
                    utc_text(now),
                    json.loads(_dump(current)),
                )
            return current

    async def adopt_legacy_session(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        principal_id: str,
        policy: SlotSessionPolicy | None = None,
        now: datetime | None = None,
    ) -> ManagedSessionSnapshot:
        """Explicit migration: keep all IDs, epochs, claims, timestamps and exact deadline."""
        now = now or utc_now()
        async with self._transaction("work_window_legacy_adopt") as db:
            await self._home(db, logical_agent_id)
            session = await self._session_on(db, work_session_id)
            if (
                session is None
                or session.logical_agent_id != logical_agent_id
                or session.session_epoch != _revision(session_epoch)
            ):
                raise WorkWindowStoreError("session_not_found")
            if session.state not in {"active", "stopping"}:
                raise WorkWindowStoreError("session_not_active")
            current = await self._current(db, logical_agent_id)
            binding = await self._binding_on(db, work_session_id)
            if binding:
                if current is None or current.work_window_id != binding.work_window_id:
                    raise WorkWindowStoreError("session_binding_invalid")
                return ManagedSessionSnapshot(current, session, binding)
            if current is not None:
                raise WorkWindowStoreError("window_migration_conflict", current=current)
            effective = policy or self.defaults
            deadline = parse_utc(session.hard_expires_at)
            original = await (
                await db.execute(
                    "SELECT MIN(started_at) FROM logical_agent_work_sessions "
                    "WHERE logical_agent_id=? AND hard_expires_at=? AND session_epoch<=?",
                    (logical_agent_id, session.hard_expires_at, session_epoch),
                )
            ).fetchone()
            original_started_at = original[0] or session.started_at
            duration = max(
                1, math.ceil((deadline - parse_utc(original_started_at)).total_seconds())
            )
            # A resumed legacy session may have fractional remaining duration.
            # Keep its exact deadline rather than rounding it into extra budget.
            window = WorkWindow.open(
                "ww_" + secrets.token_urlsafe(18),
                logical_agent_id,
                deadline - timedelta(seconds=duration),
                replace(effective, default_duration_seconds=duration),
            )
            binding = WorkSessionBinding(
                work_session_id, logical_agent_id, window.work_window_id, session_epoch, "legacy", 1
            )
            await self._insert_window(db, window)
            await self._insert_binding(db, binding)
            # The managed window becomes the sole time authority. Preserve the
            # old timer row as history, but do not let the legacy rearm loop
            # later create an independent duration budget.
            await db.execute(
                "UPDATE logical_agent_rearms SET cancelled_at=COALESCE(cancelled_at,?) "
                "WHERE logical_agent_id=? AND rearmed_at IS NULL",
                (utc_text(now), logical_agent_id),
            )
            await db.execute(
                "INSERT OR IGNORE INTO logical_agent_session_policies VALUES(?,1,?,?)",
                (logical_agent_id, json.dumps(asdict(effective)), utc_text(now)),
            )
            await self._audit(
                db,
                logical_agent_id,
                "work_window_legacy_adopted",
                principal_id,
                utc_text(now),
                {"window": json.loads(_dump(window)), "legacy_started_at": session.started_at},
                session,
            )
            return ManagedSessionSnapshot(window, session, binding, created=True)

    async def claim_window_recovery(
        self, *, limit: int = 8, lease_seconds: int = 30, now: datetime | None = None
    ) -> list[WindowRecoveryLease]:
        if type(limit) is not int or not 1 <= limit <= MAX_RECOVERY_BATCH:
            raise WorkWindowStoreError("recovery_limit_invalid")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= MAX_RECOVERY_LEASE_SECONDS:
            raise WorkWindowStoreError("recovery_lease_invalid")
        now = now or utc_now()
        stamp = utc_text(now)
        expiry = now + timedelta(seconds=lease_seconds)
        async with self._transaction("managed_recovery_claim") as db:
            rows = await (
                await db.execute(
                    "SELECT r.work_window_id,r.attempts,w.snapshot_json "
                    "FROM logical_agent_window_recovery r "
                    "JOIN logical_agent_work_windows w ON w.work_window_id=r.work_window_id "
                    "JOIN logical_agents a ON a.logical_agent_id=w.logical_agent_id "
                    "WHERE r.next_check_at<=? AND (r.lease_expires_at IS NULL "
                    "OR r.lease_expires_at<=?) AND w.superseded_at IS NULL "
                    "AND a.authority_node_id=? AND a.state NOT IN ('deleted','deleting') "
                    "ORDER BY r.next_check_at,r.work_window_id LIMIT ?",
                    (stamp, stamp, self.authority_node_id, limit),
                )
            ).fetchall()
            leases = []
            for window_id, attempts, snapshot in rows:
                token = secrets.token_urlsafe(24)
                await db.execute(
                    "UPDATE logical_agent_window_recovery SET lease_token=?,lease_expires_at=?,"
                    "attempts=MIN(attempts+1,1000000),updated_at=? WHERE work_window_id=?",
                    (token, utc_text(expiry), stamp, window_id),
                )
                leases.append(
                    WindowRecoveryLease(token, _load(snapshot), min(attempts + 1, 1000000), expiry)
                )
            return leases

    async def finish_window_recovery(
        self,
        lease: WindowRecoveryLease,
        *,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        if error_code is not None and error_code not in RECOVERY_ERRORS:
            raise WorkWindowStoreError("recovery_error_invalid")
        now = now or utc_now()
        async with self._transaction("managed_recovery_finish") as db:
            await self._home(db, lease.window.logical_agent_id)
            row = await (
                await db.execute(
                    "SELECT lease_token FROM logical_agent_window_recovery WHERE work_window_id=?",
                    (lease.window.work_window_id,),
                )
            ).fetchone()
            if row is None or row[0] != lease.token:
                return False
            current = await self._current(db, lease.window.logical_agent_id)
            if (
                current is None
                or current.work_window_id != lease.window.work_window_id
                or current.lifecycle is WindowLifecycle.COOLDOWN
            ):
                await db.execute(
                    "DELETE FROM logical_agent_window_recovery WHERE work_window_id=?",
                    (lease.window.work_window_id,),
                )
                return True
            # A late completion cannot undo an extension or new revision. Read
            # the current budget, not the ticket's historical deadline.
            live = await (
                await db.execute(
                    "SELECT state FROM logical_agent_work_sessions WHERE logical_agent_id=? "
                    "AND state IN ('active','stopping') ORDER BY session_epoch DESC LIMIT 1",
                    (current.logical_agent_id,),
                )
            ).fetchone()
            stopping = live is not None and live[0] == "stopping"
            retry = stopping or current.phase(now) is WindowPhase.EXPIRED
            if retry:
                seconds = min(180, 3 * 2 ** min(lease.attempt - 1, 6))
                due = now + timedelta(seconds=seconds)
                attempts = lease.attempt
                code = error_code or "execution_pending"
            else:
                due = current.hard_expires_at
                attempts = 0
                code = None
            await db.execute(
                "UPDATE logical_agent_window_recovery SET next_check_at=?,attempts=?,"
                "lease_token=NULL,lease_expires_at=NULL,last_error_code=?,updated_at=? "
                "WHERE work_window_id=? AND lease_token=?",
                (utc_text(due), attempts, code, utc_text(now), current.work_window_id, lease.token),
            )
            return True
