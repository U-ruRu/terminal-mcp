from __future__ import annotations

import secrets

from terminal_mcp.core.orchestration import normalize_preview
from terminal_mcp.core.persistent_admission import (
    PersistentAdmissionError,
    current_admission_context,
)
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.core.persistent_lifecycle import (
    PersistentLifecycleCoordinator,
    PersistentLifecycleError,
)
from terminal_mcp.storage.persistent_agents import PersistentStoreError


class PersistentBackend:
    """Feature-gated application service for Persistent Slots."""

    def __init__(
        self,
        service,
        lifecycle: PersistentLifecycleCoordinator,
        fleet_bridge=None,
        *,
        access_authority=None,
    ):
        self.service = service
        self.lifecycle = lifecycle
        self.repo = service.repo
        self.terminal = service.terminal
        self.task_coordinator = service.task_coordinator
        self.task_store = service.task_store
        self.fleet_bridge = fleet_bridge
        self.access_authority = access_authority

    async def _audit(
        self,
        logical_agent_id: str,
        event_type: str,
        *,
        work_session_id: str | None = None,
        session_epoch: int | None = None,
        payload: dict | None = None,
    ) -> None:
        context = current_admission_context(required=False)
        principal_id = context.principal_id if context is not None else "system"
        try:
            await self.lifecycle.store.add_audit_event(
                logical_agent_id,
                event_type,
                principal_id=principal_id,
                work_session_id=work_session_id,
                session_epoch=session_epoch,
                payload=payload,
            )
        except Exception as exc:
            if self.service.events:
                self.service.events.emit(
                    "persistent_audit_failed",
                    level="ERROR",
                    outcome="error",
                    logical_agent_id=logical_agent_id,
                    event_type=event_type,
                    error=exc.__class__.__name__,
                )

    @staticmethod
    def _error(exc: PersistentLifecycleError) -> dict:
        result = {"ok": False, "code": exc.code, "error": exc.code}
        if exc.blockers:
            result["blockers"] = exc.blockers
        return result

    async def _idempotent(
        self,
        logical_agent_id: str,
        operation: str,
        idempotency_key: str,
        request: dict,
        action,
    ):
        fingerprint = self.lifecycle.store.idempotency_fingerprint(request)
        try:
            replay = await self.lifecycle.store.idempotency_reserve(
                logical_agent_id, operation, idempotency_key, fingerprint
            )
            if replay is not None:
                return replay
            try:
                result = await action()
            except (PersistentLifecycleError, ValueError):
                await self.lifecycle.store.idempotency_abort(
                    logical_agent_id, operation, idempotency_key, fingerprint
                )
                raise
            if not result.get("ok"):
                await self.lifecycle.store.idempotency_abort(
                    logical_agent_id, operation, idempotency_key, fingerprint
                )
                return result
            result = await self.lifecycle.store.idempotency_complete(
                logical_agent_id,
                operation,
                idempotency_key,
                fingerprint,
                result,
            )
            await self._audit(
                logical_agent_id,
                operation,
                payload={"request": request},
            )
            return result
        except PersistentStoreError as exc:
            return self._error(PersistentLifecycleError(exc.code, blockers=exc.blockers))

    async def _execution_authority(
        self, logical_agent_id: str, work_session_id: str, session_epoch: int, *, scope: str
    ):
        try:
            session = await self.lifecycle.authorize_session(
                logical_agent_id, work_session_id, session_epoch
            )
            return session, None
        except PersistentLifecycleError as exc:
            if (
                exc.code not in {"session_not_found", "authority_unavailable"}
                or self.fleet_bridge is None
            ):
                raise
        try:
            verified = current_admission_context(required=True).require("terminal:execute")
        except PersistentAdmissionError as exc:
            raise PersistentLifecycleError(exc.code) from exc
        try:
            permit = await self.fleet_bridge.acquire_permit(
                logical_agent_id=logical_agent_id,
                work_session_id=work_session_id,
                session_epoch=session_epoch,
                scope=scope,
                principal_id=verified.principal_id,
            )
        except PersistentStoreError as exc:
            raise PersistentLifecycleError(exc.code, blockers=exc.blockers) from exc
        return None, permit

    async def _access_ensure(self, logical_agent_id: str, *, display_suffix: str | None = None):
        selectors = await self.lifecycle.store.all_selectors()
        slot = await self.lifecycle.store.get_slot(logical_agent_id)
        if slot is None:
            raise PersistentLifecycleError("slot_not_found")
        if self.fleet_bridge is not None:
            return await self.fleet_bridge.ensure_access_slot(
                logical_agent_id,
                slot.authority_node_id,
                display_suffix=display_suffix,
                forbidden_codes=selectors,
            )
        if self.access_authority is None:
            raise PersistentLifecycleError("authority_unavailable")
        await self.access_authority.reserve_access_codes(selectors)
        access = await self.access_authority.register_access_slot(
            logical_agent_id,
            slot.authority_node_id,
            slot_kind="persistent",
            display_suffix=display_suffix,
        )
        if int(access["access_generation"]) == 0:
            issued = await self.access_authority.issue_access_code(logical_agent_id)
            return {**access, **issued}
        return access

    async def slot_migrate_access(self, logical_agent_id: str):
        try:
            return {"ok": True, "access": await self._access_ensure(logical_agent_id)}
        except (PersistentLifecycleError, PersistentStoreError) as exc:
            code = getattr(exc, "code", "authority_unavailable")
            return {"ok": False, "code": code, "error": code}

    async def slot_rotate_access_code(self, logical_agent_id: str):
        try:
            selectors = await self.lifecycle.store.all_selectors()
            if self.fleet_bridge is not None:
                access = await self.fleet_bridge.rotate_access_code(
                    logical_agent_id, forbidden_codes=selectors
                )
            elif self.access_authority is not None:
                await self.access_authority.reserve_access_codes(selectors)
                access = await self.access_authority.issue_access_code(logical_agent_id)
            else:
                raise PersistentStoreError("authority_unavailable")
            return {"ok": True, "access": access}
        except Exception as exc:
            code = getattr(exc, "code", "access_rotation_failed")
            return {"ok": False, "code": code, "error": code}

    async def _access_get(self, logical_agent_id: str):
        try:
            if self.fleet_bridge is not None:
                return await self.fleet_bridge.get_access_slot(logical_agent_id)
            if self.access_authority is not None:
                return await self.access_authority.access_slot(logical_agent_id)
        except PersistentStoreError:
            return None
        return None

    async def _access_update_display(self, logical_agent_id: str, display_name: str):
        if self.fleet_bridge is not None:
            return await self.fleet_bridge.update_access_display_suffix(
                logical_agent_id, display_name
            )
        if self.access_authority is None:
            raise PersistentStoreError("authority_unavailable")
        return await self.access_authority.update_access_display_suffix(
            logical_agent_id, display_name
        )

    async def _access_retire(self, logical_agent_id: str):
        if self.fleet_bridge is not None:
            return await self.fleet_bridge.retire_access_slot(logical_agent_id)
        if self.access_authority is None:
            raise PersistentStoreError("authority_unavailable")
        return await self.access_authority.retire_access_slot(logical_agent_id)

    async def slot_list(self):
        try:
            result = await self.lifecycle.list_slots()
            for item in result.get("slots") or []:
                logical_agent_id = item["slot"]["logical_agent_id"]
                item["access"] = await self._access_get(logical_agent_id)
            return result
        except PersistentLifecycleError as exc:
            return self._error(exc)

    async def slot_get(self, logical_agent_id: str):
        try:
            result = await self.lifecycle.get_slot(logical_agent_id)
            result["audit"] = await self.lifecycle.store.audit_events(logical_agent_id)
            result["access"] = await self._access_get(logical_agent_id)
            return result
        except PersistentLifecycleError as exc:
            return self._error(exc)

    async def slot_create(self, display_name: str):
        try:
            result = await self.lifecycle.create_slot(display_name)
            if result.get("ok"):
                logical_agent_id = result["slot"]["logical_agent_id"]
                try:
                    result["access"] = await self._access_ensure(
                        logical_agent_id, display_suffix=display_name
                    )
                except PersistentStoreError as exc:
                    # ACCESS001 is independently deployable before ACCESS002 cutover.
                    # A remote control authority may still run the previous artifact;
                    # keep the legacy slot surface usable and expose migration debt.
                    result["access"] = {
                        "status": "migration_pending",
                        "code": exc.code,
                    }
                await self._audit(
                    logical_agent_id,
                    "create",
                    payload={"display_name": display_name},
                )
            return result
        except PersistentLifecycleError as exc:
            return self._error(exc)

    async def slot_rename(
        self,
        logical_agent_id: str,
        display_name: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ):
        try:
            async with self.lifecycle.operation_guard(logical_agent_id):
                result = await self._idempotent(
                    logical_agent_id,
                    "rename",
                    idempotency_key,
                    {"display_name": display_name, "expected_revision": expected_revision},
                    lambda: self.lifecycle.rename_slot(
                        logical_agent_id, display_name, expected_revision=expected_revision
                    ),
                )
                if result.get("ok"):
                    try:
                        result["access"] = await self._access_update_display(
                            logical_agent_id, display_name
                        )
                    except PersistentStoreError as exc:
                        result["access"] = {"status": "migration_pending", "code": exc.code}
                return result
        except PersistentLifecycleError as exc:
            return self._error(exc)

    async def slot_rotate_selector(
        self, logical_agent_id: str, selector: str, *, expected_revision: int, idempotency_key: str
    ):
        try:
            async with self.lifecycle.operation_guard(logical_agent_id):
                return await self._idempotent(
                    logical_agent_id,
                    "rotate_selector",
                    idempotency_key,
                    {"selector": selector, "expected_revision": expected_revision},
                    lambda: self.lifecycle.rotate_selector(
                        logical_agent_id, selector, expected_revision=expected_revision
                    ),
                )
        except PersistentLifecycleError as exc:
            return self._error(exc)

    async def slot_play(
        self, logical_agent_id: str, *, expected_revision: int, idempotency_key: str
    ):
        try:
            async with self.lifecycle.operation_guard(logical_agent_id):
                return await self._idempotent(
                    logical_agent_id,
                    "play",
                    idempotency_key,
                    {"expected_revision": expected_revision},
                    lambda: self.lifecycle.play(
                        logical_agent_id, expected_revision=expected_revision
                    ),
                )
        except PersistentLifecycleError as exc:
            return self._error(exc)

    async def slot_suspend(
        self, logical_agent_id: str, *, expected_revision: int, idempotency_key: str
    ):
        try:
            async with self.lifecycle.operation_guard(logical_agent_id):
                return await self._idempotent(
                    logical_agent_id,
                    "suspend",
                    idempotency_key,
                    {"expected_revision": expected_revision},
                    lambda: self.lifecycle.suspend(
                        logical_agent_id, expected_revision=expected_revision
                    ),
                )
        except PersistentLifecycleError as exc:
            return self._error(exc)

    async def slot_delete(
        self, logical_agent_id: str, *, expected_revision: int, idempotency_key: str
    ):
        try:
            async with self.lifecycle.operation_guard(logical_agent_id):
                result = await self._idempotent(
                    logical_agent_id,
                    "delete",
                    idempotency_key,
                    {"expected_revision": expected_revision},
                    lambda: self.lifecycle.delete(
                        logical_agent_id, expected_revision=expected_revision
                    ),
                )
                if result.get("ok"):
                    try:
                        result["access"] = await self._access_retire(logical_agent_id)
                    except PersistentStoreError as exc:
                        result["access"] = {"status": "retirement_pending", "code": exc.code}
                return result
        except PersistentLifecycleError as exc:
            return self._error(exc)

    async def session_start(self, selector: str, *, expected_revision: int):
        try:
            owner = await self.lifecycle.store.selector_owner(selector)
            if owner is None or owner.get("retired_at") or owner.get("tombstoned_at"):
                raise PersistentLifecycleError("slot_not_found")
            logical_agent_id = owner["logical_agent_id"]
            async with self.lifecycle.operation_guard(logical_agent_id):
                result = await self.lifecycle.session_start(
                    selector, expected_revision=expected_revision
                )
                if result.get("ok"):
                    await self._audit(
                        logical_agent_id,
                        "session_start",
                        work_session_id=result["work_session_id"],
                        session_epoch=result["session_epoch"],
                    )
                    if self.fleet_bridge is not None:
                        await self.fleet_bridge.publish_authority(
                            logical_agent_id,
                            result["work_session"]["authority_node_id"],
                            result["work_session"]["authority_epoch"],
                        )
                return result
        except ValueError as exc:
            return {"ok": False, "code": "slot_not_found", "error": str(exc)}
        except PersistentLifecycleError as exc:
            return self._error(exc)

    async def session_end(self, logical_agent_id: str, work_session_id: str, session_epoch: int):
        try:
            async with self.lifecycle.operation_guard(logical_agent_id):
                result = await self.lifecycle.session_end(
                    logical_agent_id, work_session_id, session_epoch
                )
                if result.get("ok"):
                    await self._audit(
                        logical_agent_id,
                        "session_end",
                        work_session_id=work_session_id,
                        session_epoch=session_epoch,
                    )
                return result
        except PersistentLifecycleError as exc:
            return self._error(exc)

    async def _task_refs(self, logical_agent_id: str):
        if self.task_store is None:
            return []
        claims = await self.task_store.claims_for_owner(
            ClaimOwner.logical_agent(logical_agent_id), active_only=True
        )
        return [
            {
                "namespace": item["namespace"],
                "task_id": item["task_id"],
                "lane": item["lane"],
                "priority": item["priority"],
                "state": item["state"],
                "isolation_hint": item["isolation_hint"],
            }
            for item in claims
        ]

    @staticmethod
    def _select_task_refs(available, task_scope):
        options = ["none"]
        concrete = [f"{item['namespace']}/{item['task_id']}" for item in available]
        if concrete:
            options.extend(["all", *concrete])
        if task_scope not in options:
            return None, options
        if task_scope == "all":
            return available, options
        if task_scope == "none":
            return [], options
        return [
            item for item in available if f"{item['namespace']}/{item['task_id']}" == task_scope
        ], options

    async def run(
        self,
        cmd: str,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        queue_id: int | None,
        task_scope: str,
    ):
        if not cmd:
            return {"ok": False, "code": "invalid_command", "error": "command is required"}
        try:
            async with self.lifecycle.operation_guard(logical_agent_id):
                session, permit = await self._execution_authority(
                    logical_agent_id, work_session_id, session_epoch, scope="run"
                )
                if permit is not None:
                    bridge = getattr(self, "fleet_bridge", None)
                    if bridge is not None:
                        try:
                            bridge.ensure_permit_valid(permit)
                        except PersistentStoreError as exc:
                            raise PersistentLifecycleError(exc.code, blockers=exc.blockers) from exc
                    if task_scope != "none":
                        raise PersistentLifecycleError("policy_incompatible")
                available = await self._task_refs(logical_agent_id)
                selected, options = self._select_task_refs(available, task_scope)
                if selected is None:
                    return {
                        "ok": False,
                        "code": "invalid_task_scope",
                        "error": "invalid_task_scope",
                        "task_scope_options": options,
                    }
                if queue_id is None:
                    selected_queue = await self.terminal.least_loaded_queue()
                else:
                    if queue_id < 1 or queue_id > self.terminal.queue_workers:
                        return {
                            "ok": False,
                            "code": "invalid_queue",
                            "error": (
                                f"queue_id must be between 1 and {self.terminal.queue_workers}"
                            ),
                        }
                    selected_queue = queue_id
                command = None
                for _ in range(32):
                    try:
                        command_hash = secrets.token_hex(4)
                        command = await self.repo.create(
                            cmd,
                            status="queued",
                            cmd_hash=command_hash,
                            agent_id=logical_agent_id,
                            command_type="persistent_run",
                            command_preview=normalize_preview(
                                cmd, self.service.agent_policy.command_preview_chars
                            ),
                            queue_id=selected_queue,
                            logical_agent_id=logical_agent_id,
                            work_session_id=work_session_id,
                            session_epoch=session_epoch,
                            persistent_permit=permit.as_dict() if permit is not None else None,
                        )
                        break
                    except Exception as exc:
                        if exc.__class__.__name__ != "IntegrityError":
                            raise
                if command is None:
                    raise RuntimeError("unable to allocate unique command hash")
                await self.terminal.submit(command)
                if self.task_coordinator and selected:
                    await self.task_coordinator.record_command(
                        logical_agent_id,
                        command.cmd_hash,
                        "persistent_run",
                        selected,
                        logical_agent_id=logical_agent_id,
                        work_session_id=work_session_id,
                        session_epoch=session_epoch,
                    )
                return {
                    "ok": True,
                    "cmd_hash": command.cmd_hash,
                    "queue_id": command.queue_id,
                    "queue_position": await self.repo.queue_position(command.cmd_hash),
                    "task_scope": task_scope,
                    "task_targets": [f"{item['namespace']}/{item['task_id']}" for item in selected],
                    "task_scope_options": options,
                    "logical_agent_id": logical_agent_id,
                    "work_session_id": work_session_id,
                    "session_epoch": session_epoch,
                    "hard_expires_at": (
                        session.hard_expires_at if session is not None else permit.hard_expires_at
                    ),
                    "authority_node_id": (
                        session.authority_node_id
                        if session is not None
                        else permit.authority_node_id
                    ),
                    "node_attachment_id": permit.node_attachment_id if permit is not None else None,
                    "error": None,
                }
        except PersistentLifecycleError as exc:
            return self._error(exc)
        except Exception as exc:
            return {"ok": False, "code": "run_failed", "error": str(exc)}

    async def cancel(
        self,
        cmd_hash: str,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
    ):
        try:
            async with self.lifecycle.operation_guard(logical_agent_id):
                await self._execution_authority(
                    logical_agent_id, work_session_id, session_epoch, scope="cancel"
                )
                attribution = await self.repo.persistent_attribution(cmd_hash)
                if attribution is None:
                    return {
                        "ok": False,
                        "code": "command_not_persistent",
                        "error": "command_not_persistent",
                    }
                exact = (
                    attribution["logical_agent_id"] == logical_agent_id
                    and attribution["work_session_id"] == work_session_id
                    and attribution["session_epoch"] == session_epoch
                )
                if not exact:
                    return {"ok": False, "code": "command_not_owned", "error": "command_not_owned"}
                command, cancelled_before_start = await self.repo.cancel_if_queued(cmd_hash)
                if command is None:
                    return {"ok": False, "code": "command_not_found", "error": "command_not_found"}
                if cancelled_before_start:
                    return {
                        "ok": True,
                        "cmd_hash": cmd_hash,
                        "cancelled_from": "queued",
                        "execution_started": False,
                        "error": None,
                    }
                if command.status == "cancelled":
                    return {
                        "ok": True,
                        "cmd_hash": cmd_hash,
                        "cancelled_from": "running",
                        "execution_started": True,
                        "error": None,
                    }
                if command.status != "running":
                    return {
                        "ok": False,
                        "code": "command_not_running",
                        "error": f"command is already {command.status}",
                    }
                ok, error = await self.terminal.cancel(command)
                return {
                    "ok": ok,
                    "cmd_hash": cmd_hash,
                    "cancelled_from": "running" if ok else None,
                    "execution_started": True,
                    "error": error,
                }
        except PersistentLifecycleError as exc:
            return self._error(exc)

    async def task(
        self,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        action: str,
        namespace: str,
        task_id: str | None = None,
        **kwargs,
    ):
        if self.task_coordinator is None:
            return {"ok": False, "code": "task_unavailable", "error": "task unavailable"}
        try:
            async with self.lifecycle.operation_guard(logical_agent_id):
                await self.lifecycle.authorize_session(
                    logical_agent_id, work_session_id, session_epoch
                )
                result = await self.task_coordinator.mutate(
                    logical_agent_id,
                    action=action,
                    namespace=namespace,
                    task_id=task_id,
                    _claim_owner=ClaimOwner.logical_agent(logical_agent_id),
                    **kwargs,
                )
                actual_task = result.get("task") if isinstance(result, dict) else None
                if result.get("ok") and actual_task:
                    await self.task_store.add_event(
                        actual_task["namespace"],
                        actual_task["task_id"],
                        "persistent_mutation",
                        agent_id=logical_agent_id,
                        payload={"action": action},
                        logical_agent_id=logical_agent_id,
                        work_session_id=work_session_id,
                        session_epoch=session_epoch,
                    )
                return result
        except PersistentLifecycleError as exc:
            return self._error(exc)

    async def claim_release(
        self,
        namespace: str,
        task_id: str,
        logical_agent_id: str,
        *,
        work_session_id: str,
        session_epoch: int,
    ):
        try:
            async with self.lifecycle.operation_guard(logical_agent_id):
                await self.lifecycle.authorize_session(
                    logical_agent_id, work_session_id, session_epoch
                )
                result = await self.lifecycle.claim_release(namespace, task_id, logical_agent_id)
                if result.get("ok"):
                    await self.task_store.add_event(
                        namespace,
                        task_id,
                        "persistent_claim_release",
                        agent_id=logical_agent_id,
                        payload={"released": True},
                        logical_agent_id=logical_agent_id,
                        work_session_id=work_session_id,
                        session_epoch=session_epoch,
                    )
                return result
        except PersistentLifecycleError as exc:
            return self._error(exc)

    async def claim_reassign(
        self,
        namespace: str,
        task_id: str,
        from_logical_agent_id: str,
        to_logical_agent_id: str,
        *,
        work_session_id: str,
        session_epoch: int,
        expected_revision: int,
        idempotency_key: str,
    ):
        try:
            async with self.lifecycle.operation_guard(from_logical_agent_id):
                await self.lifecycle.authorize_session(
                    from_logical_agent_id, work_session_id, session_epoch
                )
                slot = await self.lifecycle.store.get_slot(from_logical_agent_id)
                if slot is None or slot.slot_revision != expected_revision:
                    raise PersistentLifecycleError("revision_conflict")
                result = await self._idempotent(
                    from_logical_agent_id,
                    "claim_reassign",
                    idempotency_key,
                    {
                        "namespace": namespace,
                        "task_id": task_id,
                        "to_logical_agent_id": to_logical_agent_id,
                        "expected_revision": expected_revision,
                    },
                    lambda: self.lifecycle.claim_reassign(
                        namespace, task_id, from_logical_agent_id, to_logical_agent_id
                    ),
                )
                if result.get("ok"):
                    await self.task_store.add_event(
                        namespace,
                        task_id,
                        "persistent_claim_reassign",
                        agent_id=from_logical_agent_id,
                        payload={"to_logical_agent_id": to_logical_agent_id},
                        logical_agent_id=from_logical_agent_id,
                        work_session_id=work_session_id,
                        session_epoch=session_epoch,
                    )
                return result
        except PersistentLifecycleError as exc:
            return self._error(exc)
