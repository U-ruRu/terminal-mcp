from __future__ import annotations

import asyncio
import secrets

from terminal_mcp.core.access_mesh_scope import REGISTRY_NAMESPACE
from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.core.native_admission import (
    NativeWriteAdmission,
    NativeWriteAdmissionError,
    native_write_admission,
)
from terminal_mcp.core.orchestration import normalize_preview, utc_text
from terminal_mcp.core.persistent_admission import (
    current_admission_context,
)
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.core.public_errors import normalize_public_error
from terminal_mcp.core.session_execution import (
    SessionExecutionCoordinator,
    SessionExecutionError,
)
from terminal_mcp.storage.persistent_agents import PersistentStoreError

COMMAND_RUN_INLINE_BUDGET_SECONDS = 5.0
COMMAND_RUN_INLINE_POLL_SECONDS = 0.05
COMMAND_RUN_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


class PersistentBackend:
    """Command, task and inbox operations under the native Access session fence."""

    def __init__(
        self,
        service,
        lifecycle: SessionExecutionCoordinator,
    ):
        self.service = service
        self.lifecycle = lifecycle
        self.repo = service.repo
        self.terminal = service.terminal
        self.task_coordinator = service.task_coordinator
        self.task_store = service.task_store

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
    def _error(exc: SessionExecutionError | NativeWriteAdmissionError) -> dict:
        result = {"ok": False, "code": exc.code, "error": exc.code}
        if getattr(exc, "blockers", None):
            result["blockers"] = exc.blockers
        if isinstance(exc, NativeWriteAdmissionError):
            result["outcome"] = "not_committed"
        return result

    async def _idempotent(
        self,
        logical_agent_id: str,
        operation: str,
        idempotency_key: str,
        request: dict,
        action,
        *,
        preserve_uncertain: bool = False,
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
            except (SessionExecutionError, ValueError):
                if not preserve_uncertain:
                    await self.lifecycle.store.idempotency_abort(
                        logical_agent_id, operation, idempotency_key, fingerprint
                    )
                raise
            if not result.get("ok"):
                if (
                    not preserve_uncertain
                    or normalize_public_error(result).outcome == "not_committed"
                ):
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
            # A committed receipt is authoritative even if the optional audit
            # sink fails afterwards.
            try:
                await self._audit(
                    logical_agent_id,
                    operation,
                    payload={"request": request},
                )
            except Exception:
                # Never lose the committed receipt due to a failed secondary
                # diagnostics sink (including the diagnostics emitter itself).
                try:
                    events = getattr(getattr(self, "service", None), "events", None)
                    if events is not None:
                        events.emit(
                            "postcommit_audit_failed",
                            level="ERROR",
                            outcome="error",
                            logical_agent_id=logical_agent_id,
                            operation=operation,
                        )
                except Exception:
                    pass
            return result
        except PersistentStoreError as exc:
            return self._error(SessionExecutionError(exc.code, blockers=exc.blockers))

    def _capture_write_admission(
        self, logical_agent_id, work_session_id, session_epoch, operation
    ) -> NativeWriteAdmission:
        """Pin the namespace before resolve or any other asynchronous preflight."""
        mesh = getattr(self.service, "access_mesh", None)
        if mesh is None:
            raise SessionExecutionError("session_attach_required")
        return NativeWriteAdmission(
            namespace_id=REGISTRY_NAMESPACE.get() or mesh.store.namespace_id,
            logical_agent_id=logical_agent_id,
            work_session_id=work_session_id,
            session_epoch=session_epoch,
            operation=operation,
            clock=mesh.clock,
        )

    async def _execution_authority(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        scope: str,
        access_code: str | None = None,
    ):
        from terminal_mcp.application.actor import current_actor
        from terminal_mcp.core.access_mesh_grants import AccessMeshError
        from terminal_mcp.core.managed_sessions import ManagedOperation

        mesh = getattr(self.service, "access_mesh", None)
        actor = current_actor()
        if mesh is None or actor is None or access_code is not None:
            raise SessionExecutionError("session_attach_required")
        operation = {
            "run": ManagedOperation.COMMAND_RUN,
            "recovery": ManagedOperation.COMMAND_RECOVERY,
            "cancel": ManagedOperation.COMMAND_CANCEL,
            "message": ManagedOperation.MESSAGE_READ,
            "task": ManagedOperation.TASK_COMMENT,
        }.get(scope)
        if operation is None:
            operation = ManagedOperation(scope)
        try:
            identity = await mesh.resolve(actor, operation)
        except AccessMeshError as exc:
            raise SessionExecutionError(exc.code) from exc
        if (
            identity["logical_agent_id"],
            identity.get("work_session_id"),
            identity.get("session_epoch"),
        ) != (logical_agent_id, work_session_id, session_epoch):
            raise SessionExecutionError("stale_session_epoch")
        session = await self.lifecycle.store.get_work_session(work_session_id)
        if session is None:
            raise SessionExecutionError("session_not_found")
        return session

    async def _persistent_message_entry(self, item: dict) -> dict:
        sender_id = str(item.get("sender_agent_id") or "")
        mesh = self.service.access_mesh
        sender_slot = (
            await asyncio.to_thread(mesh.store.slot_for_agent, sender_id) if sender_id else None
        )
        alert = bool(item.get("alert"))
        require_ack = bool(item.get("require_reply")) and not alert
        declared_mode = item.get("delivery_mode") or item.get("mode")
        mode = (
            str(declared_mode)
            if declared_mode in {"notify", "ack", "alert"}
            else "alert"
            if alert
            else "ack"
            if require_ack
            else "notify"
        )
        if item.get("replied_at"):
            state = "replied"
        elif item.get("read_at"):
            state = "read"
        elif item.get("first_seen_at"):
            state = "seen"
        else:
            state = "delivered"
        return {
            "message_hash": item.get("message_ref") or item.get("message_hash"),
            "sender": (
                mesh.store.human_name(sender_slot) if sender_slot is not None else sender_id
            ),
            "text": item.get("text"),
            "mode": mode,
            "state": state,
            "created_at": item.get("created_at"),
            "first_seen_at": item.get("first_seen_at"),
            "last_seen_at": item.get("last_seen_at"),
            "seen_count": int(item.get("seen_count") or 0),
            "read_at": item.get("read_at"),
            "replied_at": item.get("replied_at"),
            "reply_message_hash": item.get("reply_message_ref") or item.get("reply_message_hash"),
            "namespace": item.get("task_namespace") or item.get("namespace"),
            "task_id": item.get("task_id"),
        }

    async def message_state(
        self,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        surface: bool = False,
    ) -> dict:
        store = self.service.agent_store
        if store is None:
            return {
                "messages": [],
                "pending_messages": [],
                "ack_required_pending": False,
                "alert_pending": False,
            }
        visible = await store.message_obligations(logical_agent_id)
        if surface and visible:
            await store.mark_messages_seen(
                logical_agent_id,
                [item["message_hash"] for item in visible],
                utc_text(),
            )
        refreshed = await store.message_obligations(logical_agent_id)
        journal = await store.message_journal(logical_agent_id, limit=500)
        current = {item["message_hash"]: item for item in journal}
        messages = [
            await self._persistent_message_entry(current.get(item["message_hash"], item))
            for item in visible
        ]
        return {
            "messages": messages,
            "pending_messages": messages,
            "ack_required_pending": any(
                item.get("delivery_mode") == "ack" and item["read_at"] is None for item in refreshed
            ),
            "alert_pending": any(
                (item["alert"] or item.get("delivery_mode") == "alert")
                and item["replied_at"] is None
                for item in refreshed
            ),
        }

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

    async def replay_command(
        self,
        action: str,
        cmd: str,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        idempotency_key: str,
        access_code: str | None = None,
        queue_id: int | None = None,
        task_scope: str = "none",
    ):
        """Reuse a durable launch receipt after rechecking the caller's execution fence."""
        if action not in {"run", "recovery"}:
            return {
                "ok": False,
                "code": "input_validation_failed",
                "error": "Unsupported command action.",
            }
        try:
            async with self.lifecycle.operation_guard(logical_agent_id):
                await self._execution_authority(
                    logical_agent_id,
                    work_session_id,
                    session_epoch,
                    scope=action,
                    access_code=access_code,
                )
        except SessionExecutionError as exc:
            return self._error(exc)

        async def launch_once():
            identity = {
                "logical_agent_id": logical_agent_id,
                "work_session_id": work_session_id,
                "session_epoch": session_epoch,
                "access_code": access_code,
            }
            if action == "run":
                return await self.run(cmd, queue_id=queue_id, task_scope=task_scope, **identity)
            return await self.recovery(cmd, **identity)

        return await launch_once()

    async def run(
        self,
        cmd: str,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        access_code: str | None = None,
        queue_id: int | None,
        task_scope: str,
    ):
        command = None
        if not cmd:
            return {
                "ok": False,
                "code": "input_validation_failed",
                "error": "command is required",
                "path": "command",
            }
        try:
            admission = self._capture_write_admission(
                logical_agent_id, work_session_id, session_epoch, ManagedOperation.COMMAND_RUN
            )
            async with self.lifecycle.operation_guard(logical_agent_id):
                session = await self._execution_authority(
                    logical_agent_id,
                    work_session_id,
                    session_epoch,
                    scope="run",
                    access_code=access_code,
                )
                available = await self._task_refs(logical_agent_id)
                selected, options = self._select_task_refs(available, task_scope)
                if selected is None:
                    return {
                        "ok": False,
                        "code": "input_validation_failed",
                        "error": "invalid_task_scope",
                        "path": "task_scope",
                        "task_scope_options": options,
                    }
                if queue_id is None:
                    selected_queue = await self.terminal.least_loaded_queue()
                else:
                    if queue_id < 1 or queue_id > self.terminal.queue_workers:
                        return {
                            "ok": False,
                            "code": "input_validation_failed",
                            "path": "queue_id",
                            "error": (
                                f"queue_id must be between 1 and {self.terminal.queue_workers}"
                            ),
                        }
                    selected_queue = queue_id
                command = None
                for _ in range(32):
                    try:
                        command_hash = secrets.token_hex(4)
                        with native_write_admission(admission):
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
                            )
                        break
                    except Exception as exc:
                        if exc.__class__.__name__ != "IntegrityError":
                            raise
                if command is None:
                    raise RuntimeError("unable to allocate unique command hash")
                await self.terminal.submit(command)
                if self.task_coordinator and selected:
                    with native_write_admission(admission):
                        await self.task_coordinator.record_command(
                            logical_agent_id,
                            command.cmd_hash,
                            "persistent_run",
                            selected,
                            logical_agent_id=logical_agent_id,
                            work_session_id=work_session_id,
                            session_epoch=session_epoch,
                        )
            current = await self.repo.get(command.cmd_hash)
            queue_position = await self.repo.queue_position(command.cmd_hash)
            current = current or command
            return {
                "ok": True,
                "cmd_hash": command.cmd_hash,
                "status": current.status,
                "queue_id": command.queue_id,
                "queue_position": queue_position,
                "exit_code": current.exit_code,
                "execution_started": bool(current.claimed_at or current.started_at),
                "claimed_at": current.claimed_at,
                "started_at": current.started_at,
                "finished_at": current.finished_at,
                "task_scope": task_scope,
                "task_targets": [f"{item['namespace']}/{item['task_id']}" for item in selected],
                "task_scope_options": options,
                "logical_agent_id": logical_agent_id,
                "work_session_id": work_session_id,
                "session_epoch": session_epoch,
                "hard_expires_at": (session.hard_expires_at),
                "authority_node_id": (session.authority_node_id),
                "error": None,
            }
        except Exception as exc:
            if command is not None:
                # The command was committed. Its hash is the recovery receipt;
                # failures in submission, task audit or inline readback cannot
                # be reported as an uncommitted command.
                return {
                    "ok": True,
                    "cmd_hash": command.cmd_hash,
                    "status": command.status,
                    "queue_id": command.queue_id,
                    "execution_started": bool(command.claimed_at or command.started_at),
                    "postcommit_warning": type(exc).__name__,
                }
            if isinstance(exc, (SessionExecutionError, NativeWriteAdmissionError)):
                return self._error(exc)
            return {"ok": False, "code": "run_failed", "error": str(exc)}

    async def recovery(
        self,
        cmd: str,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        access_code: str | None = None,
    ):
        if not cmd:
            return {
                "ok": False,
                "code": "input_validation_failed",
                "error": "command is required",
                "path": "command",
            }
        command = None
        try:
            admission = self._capture_write_admission(
                logical_agent_id, work_session_id, session_epoch, ManagedOperation.COMMAND_RECOVERY
            )
            async with self.lifecycle.operation_guard(logical_agent_id):
                await self._execution_authority(
                    logical_agent_id,
                    work_session_id,
                    session_epoch,
                    scope="recovery",
                    access_code=access_code,
                )
                for _ in range(32):
                    try:
                        with native_write_admission(admission):
                            command = await self.repo.create(
                                cmd,
                                status="running",
                                cmd_hash=secrets.token_hex(4),
                                agent_id=logical_agent_id,
                                command_type="persistent_recovery",
                                command_preview=normalize_preview(
                                    cmd, self.service.agent_policy.command_preview_chars
                                ),
                                queue_id=None,
                                logical_agent_id=logical_agent_id,
                                work_session_id=work_session_id,
                                session_epoch=session_epoch,
                            )
                        break
                    except Exception as exc:
                        if exc.__class__.__name__ != "IntegrityError":
                            raise
                if command is None:
                    raise RuntimeError("unable to allocate unique command hash")
            with native_write_admission(admission):
                duration_ms = await self.terminal.recovery(command, timeout_seconds=20)
            current = await self.repo.get(command.cmd_hash) or command
            total = await self.repo.count_lines(command.cmd_hash)
            start = max(total - 500, 0)
            lines = await self.repo.read_command_lines(command.cmd_hash, 500, start)
            output_status = await self.repo.output_status(command.cmd_hash)
            return {
                "ok": current.error is None and current.status in {"completed", "failed"},
                "cmd_hash": command.cmd_hash,
                "lines": self.service._render(lines, scoped=True),
                "overall_lines_count": total,
                "displayed_lines_count": len(lines),
                "exit_code": current.exit_code,
                "error": current.error,
                "duration_ms": duration_ms,
                **output_status,
            }
        except asyncio.CancelledError:
            raise
        except (SessionExecutionError, NativeWriteAdmissionError) as exc:
            return self._error(exc)
        except Exception as exc:
            if command is not None:
                await self.terminal.finalize_running(
                    command, "failed", command.exit_code, f"recovery.execute: {exc}"
                )
            return {"ok": False, "code": "recovery_failed", "error": str(exc)}

    async def cancel(
        self,
        cmd_hash: str,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        access_code: str | None = None,
    ):
        accepted_receipt = None
        try:
            async with self.lifecycle.operation_guard(logical_agent_id):
                await self._execution_authority(
                    logical_agent_id,
                    work_session_id,
                    session_epoch,
                    scope="cancel",
                    access_code=access_code,
                )
                attribution = await self.repo.persistent_attribution(cmd_hash)
                if attribution is None:
                    return {
                        "ok": False,
                        "code": "command_not_found",
                        "error": "command_not_found",
                    }
                exact = (
                    attribution["logical_agent_id"] == logical_agent_id
                    and attribution["work_session_id"] == work_session_id
                    and attribution["session_epoch"] == session_epoch
                )
                if not exact:
                    return {"ok": False, "code": "command_not_owned", "error": "command_not_owned"}
                command, outcome = await self.repo.accept_cancel_intent(cmd_hash)
                if command is None:
                    return {
                        "ok": False,
                        "code": "command_not_found",
                        "error": "command_not_found",
                    }
                if outcome == "command_already_finished":
                    return {
                        "ok": False,
                        "code": "command_already_finished",
                        "error": f"command is already {command.status}",
                    }
                execution_started = bool(command.claimed_at or command.started_at)
                # A retry reads the already-cancelled row, not the original queued row.
                # Its execution timestamps preserve cancellation-before-start provenance.
                cancelled_before_start = outcome == "queued" or (
                    outcome == "previously_accepted"
                    and command.status == "cancelled"
                    and not execution_started
                )
                accepted_receipt = {
                    "ok": True,
                    "cmd_hash": cmd_hash,
                    "status": "cancelled" if outcome == "queued" else command.status,
                    "cancel_requested": True,
                    "cancelled_from": "queued" if cancelled_before_start else "running",
                    "execution_started": execution_started,
                    "error": None,
                }
                if outcome == "running" or (
                    outcome == "previously_accepted" and command.status == "running"
                ):
                    self.terminal.enqueue_cancel(command)
                return accepted_receipt
        except Exception as exc:
            if accepted_receipt is not None:
                # The durable intent is authoritative. The scheduler reconciler
                # retries process cancellation even if this immediate wake-up fails.
                return {**accepted_receipt, "postcommit_warning": type(exc).__name__}
            if isinstance(exc, SessionExecutionError):
                return self._error(exc)
            raise

    async def task(
        self,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        access_code: str | None = None,
        action: str,
        namespace: str,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        **kwargs,
    ):
        if self.task_coordinator is None:
            return {"ok": False, "code": "task_unavailable", "error": "task unavailable"}
        try:
            admission = self._capture_write_admission(
                logical_agent_id, work_session_id, session_epoch, ManagedOperation(f"task.{action}")
            )
            async with self.lifecycle.operation_guard(logical_agent_id):
                await self._execution_authority(
                    logical_agent_id,
                    work_session_id,
                    session_epoch,
                    scope=f"task.{action}",
                    access_code=access_code,
                )

                claim_lease = None

                async def mutate_once():
                    result = await self.task_coordinator.mutate(
                        logical_agent_id,
                        action=action,
                        namespace=namespace,
                        task_id=task_id,
                        _claim_owner=ClaimOwner.logical_agent(logical_agent_id),
                        _claim_lease=claim_lease,
                        **kwargs,
                    )
                    actual_task = result.get("task") if isinstance(result, dict) else None
                    # Returning an already committed create receipt must not
                    # append a second persistent audit event.
                    replayed_create = action == "create" and getattr(result, "replayed", False)
                    if result.get("ok") and actual_task and not replayed_create:
                        try:
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
                        except Exception as exc:
                            if self.service.events:
                                self.service.events.emit(
                                    "task_postcommit_audit_failed",
                                    level="ERROR",
                                    outcome="error",
                                    logical_agent_id=logical_agent_id,
                                    action=action,
                                    error=exc.__class__.__name__,
                                )
                        # The coordinator captured this receipt in its write
                        # transaction. A later list snapshot may have a newer
                        # revision and must not replace the committed result.
                    return result

                with native_write_admission(admission):
                    return await mutate_once()
        except (SessionExecutionError, NativeWriteAdmissionError) as exc:
            return self._error(exc)
        except PersistentStoreError as exc:
            return {"ok": False, "code": exc.code, "error": exc.code}
