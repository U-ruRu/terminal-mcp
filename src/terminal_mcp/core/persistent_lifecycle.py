from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Protocol

from terminal_mcp.core.orchestration import parse_utc, utc_now, utc_text
from terminal_mcp.core.persistent_admission import (
    PERSISTENT_ADMISSION_SCOPE,
    PersistentAdmissionError,
    VerifiedAdmissionContext,
    current_admission_context,
)
from terminal_mcp.core.persistent_agents import generate_slot_selector
from terminal_mcp.storage.persistent_agents import PersistentAgentStore, PersistentStoreError


class PersistentLifecycleError(RuntimeError):
    def __init__(self, code: str, *, blockers: list[dict] | None = None):
        self.code = code
        self.blockers = blockers or []
        super().__init__(code)


class ExecutionFence(Protocol):
    async def revoke_session(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        reason: str,
    ) -> list[dict]: ...

    async def blockers_for_slot(self, logical_agent_id: str) -> list[dict]: ...

    async def blockers_for_claim(
        self, namespace: str, task_id: str, logical_agent_id: str
    ) -> list[dict]: ...


class NoopExecutionFence:
    async def revoke_session(self, *args, **kwargs) -> list[dict]:
        return []

    async def blockers_for_slot(self, logical_agent_id: str) -> list[dict]:
        return []

    async def blockers_for_claim(
        self, namespace: str, task_id: str, logical_agent_id: str
    ) -> list[dict]:
        return []


class PersistentLifecycleCoordinator:
    """Server-authoritative Persistent Slot lifecycle.

    Slot/session transitions are stored transactionally. Execution cancellation/drain is
    injected as a fence so lifecycle authority is revoked before OS work is drained.
    """

    def __init__(
        self,
        store: PersistentAgentStore,
        *,
        enabled: bool,
        authority_node_id: str,
        session_duration_seconds: int,
        execution_fence: ExecutionFence | None = None,
    ):
        if session_duration_seconds < 1:
            raise ValueError("session_duration_seconds must be positive")
        self.store = store
        self.enabled = bool(enabled)
        self.authority_node_id = authority_node_id or "local"
        self.session_duration_seconds = int(session_duration_seconds)
        self.execution_fence = execution_fence or NoopExecutionFence()
        self._operation_locks: dict[str, asyncio.Lock] = {}
        self._policy_lock = asyncio.Lock()
        self._reconcile_task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    def _lock(self, logical_agent_id: str) -> asyncio.Lock:
        lock = self._operation_locks.get(logical_agent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._operation_locks[logical_agent_id] = lock
        return lock

    @asynccontextmanager
    async def operation_guard(self, logical_agent_id: str):
        async with self._lock(logical_agent_id):
            yield

    @asynccontextmanager
    async def policy_guard(self):
        async with self._policy_lock:
            yield

    async def start(self, interval_seconds: float = 1.0) -> None:
        if not self.enabled or self._reconcile_task is not None:
            return
        self._stopped.clear()
        await self.reconcile_expired()
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(max(0.2, float(interval_seconds))),
            name="persistent-session-reconciler",
        )

    async def stop(self) -> None:
        self._stopped.set()
        task = self._reconcile_task
        self._reconcile_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _reconcile_loop(self, interval_seconds: float) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.sleep(interval_seconds)
                await self.reconcile_expired()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(interval_seconds)

    def _available(self) -> None:
        if not self.enabled:
            raise PersistentLifecycleError("policy_incompatible")

    @staticmethod
    def _read_admission(
        context: VerifiedAdmissionContext | None = None,
    ) -> VerifiedAdmissionContext:
        try:
            candidate = context or current_admission_context(required=True)
            return candidate.require("terminal:read")
        except PersistentAdmissionError as exc:
            raise PersistentLifecycleError(exc.code) from exc

    @staticmethod
    def _admission(
        context: VerifiedAdmissionContext | None = None,
    ) -> VerifiedAdmissionContext:
        try:
            candidate = context or current_admission_context(required=True)
            return candidate.require(PERSISTENT_ADMISSION_SCOPE)
        except PersistentAdmissionError as exc:
            raise PersistentLifecycleError(exc.code) from exc

    @staticmethod
    def _raise_store(exc: PersistentStoreError):
        raise PersistentLifecycleError(exc.code, blockers=exc.blockers) from exc

    async def _home_slot(self, logical_agent_id: str):
        slot = await self.store.get_slot(logical_agent_id)
        if slot is None or slot.state == "deleted":
            raise PersistentLifecycleError("slot_not_found")
        if slot.authority_node_id != self.authority_node_id:
            raise PersistentLifecycleError("authority_unavailable")
        return slot

    async def _slot_result(self, logical_agent_id: str) -> dict:
        slot = await self.store.get_slot(logical_agent_id)
        if slot is None:
            raise PersistentLifecycleError("slot_not_found")
        selector = await self.store.active_selector(logical_agent_id)
        session = await self.store.active_session_for_slot(logical_agent_id)
        return {
            "slot": asdict(slot),
            "selector": selector,
            "work_session": asdict(session) if session else None,
            "server_now": utc_text(),
        }

    async def list_slots(self) -> dict:
        self._available()
        self._read_admission()
        rows = []
        for slot in await self.store.list_slots():
            current = await self._slot_result(slot.logical_agent_id)
            rows.append(current)
        return {"ok": True, "slots": rows, "server_now": utc_text()}

    async def get_slot(self, logical_agent_id: str) -> dict:
        self._available()
        self._read_admission()
        return {"ok": True, **(await self._slot_result(logical_agent_id))}

    async def create_slot(
        self,
        display_name: str,
        *,
        admission: VerifiedAdmissionContext | None = None,
    ) -> dict:
        self._available()
        self._admission(admission)
        logical_agent_id = "la_" + secrets.token_urlsafe(18)
        last_error = None
        for _ in range(32):
            selector = generate_slot_selector()
            try:
                slot = await self.store.create_slot(
                    logical_agent_id,
                    display_name.strip(),
                    selector,
                    authority_node_id=self.authority_node_id,
                )
                return {
                    "ok": True,
                    "slot": asdict(slot),
                    "selector": {"selector": selector, "generation": 1},
                    "server_now": utc_text(),
                }
            except Exception as exc:
                if "UNIQUE constraint failed: logical_agent_selectors.selector" not in str(exc):
                    raise
                last_error = exc
        raise PersistentLifecycleError("policy_incompatible") from last_error

    async def rename_slot(
        self,
        logical_agent_id: str,
        display_name: str,
        *,
        expected_revision: int,
        admission: VerifiedAdmissionContext | None = None,
    ) -> dict:
        self._available()
        self._admission(admission)
        await self._home_slot(logical_agent_id)
        try:
            await self.store.rename_slot(
                logical_agent_id, display_name, expected_revision=expected_revision
            )
        except PersistentStoreError as exc:
            self._raise_store(exc)
        return {"ok": True, **(await self._slot_result(logical_agent_id))}

    async def rotate_selector(
        self,
        logical_agent_id: str,
        selector: str,
        *,
        expected_revision: int,
        admission: VerifiedAdmissionContext | None = None,
    ) -> dict:
        self._available()
        self._admission(admission)
        slot = await self._home_slot(logical_agent_id)
        if slot.slot_revision != expected_revision:
            raise PersistentLifecycleError("revision_conflict")
        if slot.state in {"active", "stopping"}:
            raise PersistentLifecycleError("session_already_active")
        try:
            await self.store.rotate_selector(
                logical_agent_id,
                selector,
                expected_generation=slot.selector_generation,
            )
        except ValueError as exc:
            if str(exc) == "selector generation conflict":
                raise PersistentLifecycleError("revision_conflict") from exc
            raise
        return {"ok": True, **(await self._slot_result(logical_agent_id))}

    async def play(
        self,
        logical_agent_id: str,
        *,
        expected_revision: int,
        admission: VerifiedAdmissionContext | None = None,
    ) -> dict:
        self._available()
        self._admission(admission)
        await self._home_slot(logical_agent_id)
        try:
            async with self.policy_guard():
                slot, arm = await self.store.arm_slot(
                    logical_agent_id,
                    self.session_duration_seconds,
                    expected_revision=expected_revision,
                )
        except PersistentStoreError as exc:
            self._raise_store(exc)
        return {
            "ok": True,
            "slot": asdict(slot),
            "arm": asdict(arm),
            "server_now": utc_text(),
        }

    async def session_start(
        self,
        selector: str,
        *,
        expected_revision: int,
        admission: VerifiedAdmissionContext | None = None,
        origin_instance_id: str | None = None,
    ) -> dict:
        self._available()
        verified = self._admission(admission)
        try:
            slot, session = await self.store.start_session(
                selector=selector,
                work_session_id="ws_" + secrets.token_urlsafe(18),
                expected_revision=expected_revision,
                principal_id=verified.principal_id,
                auth_generation=verified.auth_generation,
                authority_node_id=self.authority_node_id,
                origin_instance_id=origin_instance_id or self.authority_node_id,
            )
        except PersistentStoreError as exc:
            self._raise_store(exc)
        return {
            "ok": True,
            "logical_agent_id": slot.logical_agent_id,
            "work_session_id": session.work_session_id,
            "session_epoch": session.session_epoch,
            "hard_expires_at": session.hard_expires_at,
            "slot_revision": slot.slot_revision,
            "slot": asdict(slot),
            "work_session": asdict(session),
            "server_now": utc_text(),
        }

    async def authorize_session(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        admission: VerifiedAdmissionContext | None = None,
        now: str | None = None,
    ):
        self._available()
        verified = self._admission(admission)
        try:
            session = await self.store.assert_session_authority(
                logical_agent_id, work_session_id, session_epoch, now=now
            )
        except PersistentStoreError as exc:
            self._raise_store(exc)
        if session.authority_node_id != self.authority_node_id:
            raise PersistentLifecycleError("authority_unavailable")
        if session.auth_principal_id and session.auth_principal_id != verified.principal_id:
            raise PersistentLifecycleError("persistent_auth_required")
        return session

    async def _stop_active_session(
        self,
        logical_agent_id: str,
        session,
        *,
        reason: str,
        terminal_state: str,
    ) -> dict:
        try:
            await self.store.begin_session_stop(
                logical_agent_id,
                session.work_session_id,
                session.session_epoch,
                reason=reason,
            )
        except PersistentStoreError as exc:
            self._raise_store(exc)
        blockers = await self.execution_fence.revoke_session(
            logical_agent_id,
            session.work_session_id,
            session.session_epoch,
            reason=reason,
        )
        if blockers:
            result = await self._slot_result(logical_agent_id)
            return {"ok": True, "stopping": True, "blockers": blockers, **result}
        try:
            slot, ended = await self.store.finalize_session_stop(
                logical_agent_id,
                session.work_session_id,
                session.session_epoch,
                reason=reason,
                terminal_state=terminal_state,
            )
        except PersistentStoreError as exc:
            self._raise_store(exc)
        return {
            "ok": True,
            "stopping": False,
            "slot": asdict(slot),
            "work_session": asdict(ended),
            "server_now": utc_text(),
        }

    async def session_end(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        admission: VerifiedAdmissionContext | None = None,
    ) -> dict:
        await self.authorize_session(
            logical_agent_id,
            work_session_id,
            session_epoch,
            admission=admission,
        )
        session = await self.store.get_work_session(work_session_id)
        return await self._stop_active_session(
            logical_agent_id, session, reason="session_end", terminal_state="ended"
        )

    async def suspend(
        self,
        logical_agent_id: str,
        *,
        expected_revision: int,
        admission: VerifiedAdmissionContext | None = None,
    ) -> dict:
        self._available()
        self._admission(admission)
        slot = await self._home_slot(logical_agent_id)
        if slot.slot_revision != expected_revision:
            raise PersistentLifecycleError("revision_conflict")
        session = await self.store.active_session_for_slot(logical_agent_id)
        if session:
            return await self._stop_active_session(
                logical_agent_id, session, reason="suspend", terminal_state="suspended"
            )
        try:
            slot = await self.store.suspend_non_active(
                logical_agent_id, expected_revision=expected_revision
            )
        except PersistentStoreError as exc:
            self._raise_store(exc)
        return {"ok": True, "slot": asdict(slot), "server_now": utc_text()}

    async def delete(
        self,
        logical_agent_id: str,
        *,
        expected_revision: int,
        admission: VerifiedAdmissionContext | None = None,
    ) -> dict:
        self._available()
        self._admission(admission)
        await self._home_slot(logical_agent_id)
        blockers = await self.execution_fence.blockers_for_slot(logical_agent_id)
        if blockers:
            raise PersistentLifecycleError("delete_blocked", blockers=blockers)
        try:
            slot = await self.store.delete_slot(
                logical_agent_id, expected_revision=expected_revision
            )
        except PersistentStoreError as exc:
            self._raise_store(exc)
        return {"ok": True, "slot": asdict(slot), "server_now": utc_text()}

    async def claim_release(
        self,
        namespace: str,
        task_id: str,
        logical_agent_id: str,
        *,
        admission: VerifiedAdmissionContext | None = None,
    ) -> dict:
        self._available()
        self._admission(admission)
        await self._home_slot(logical_agent_id)
        changed = await self.store.release_logical_claim(namespace, task_id, logical_agent_id)
        return {"ok": changed, "released": changed}

    async def claim_reassign(
        self,
        namespace: str,
        task_id: str,
        from_logical_agent_id: str,
        to_logical_agent_id: str,
        *,
        admission: VerifiedAdmissionContext | None = None,
    ) -> dict:
        self._available()
        self._admission(admission)
        await self._home_slot(from_logical_agent_id)
        blockers = await self.execution_fence.blockers_for_claim(
            namespace, task_id, from_logical_agent_id
        )
        if blockers:
            raise PersistentLifecycleError("reassign_blocked", blockers=blockers)
        try:
            await self.store.reassign_logical_claim(
                namespace, task_id, from_logical_agent_id, to_logical_agent_id
            )
        except PersistentStoreError as exc:
            self._raise_store(exc)
        return {
            "ok": True,
            "namespace": namespace,
            "task_id": task_id,
            "from_logical_agent_id": from_logical_agent_id,
            "to_logical_agent_id": to_logical_agent_id,
        }

    async def reconcile_expired(self, *, now=None) -> list[dict]:
        self._available()
        current = now or utc_now()
        reconciled = []
        for slot in await self.store.list_slots():
            if slot.authority_node_id != self.authority_node_id:
                continue
            session = await self.store.active_session_for_slot(slot.logical_agent_id)
            if not session or session.state not in {"active", "stopping"}:
                continue
            if session.state == "active" and current < parse_utc(session.hard_expires_at):
                continue
            async with self.operation_guard(slot.logical_agent_id):
                refreshed = await self.store.active_session_for_slot(slot.logical_agent_id)
                if not refreshed:
                    continue
                if refreshed.state == "active" and current < parse_utc(refreshed.hard_expires_at):
                    continue
                result = await self._stop_active_session(
                    slot.logical_agent_id,
                    refreshed,
                    reason=(
                        "hard_duration"
                        if refreshed.state == "active"
                        else (refreshed.end_reason or "stopping")
                    ),
                    terminal_state="expired" if refreshed.state == "active" else "ended",
                )
                reconciled.append(result)
        return reconciled
