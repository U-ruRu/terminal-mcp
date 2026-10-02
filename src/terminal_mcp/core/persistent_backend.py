from __future__ import annotations

import asyncio
import secrets

from terminal_mcp.core.orchestration import normalize_preview, utc_text
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
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        scope: str,
        access_code: str | None = None,
    ):
        try:
            session = await self.lifecycle.authorize_session(
                logical_agent_id,
                work_session_id,
                session_epoch,
                access_code_verified=access_code is not None,
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
                access_code=access_code,
            )
        except PersistentStoreError as exc:
            raise PersistentLifecycleError(exc.code, blockers=exc.blockers) from exc
        return None, permit

    async def _access_ensure(
        self,
        logical_agent_id: str,
        *,
        display_suffix: str | None = None,
        slot_kind: str = "persistent",
    ):
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
                slot_kind=slot_kind,
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

    async def _resolve_access_name(self, public_name: str) -> dict:
        if self.fleet_bridge is not None:
            access = await self.fleet_bridge.get_access_slot_by_public_name(public_name)
        elif self.access_authority is not None:
            access = await self.access_authority.access_slot_by_public_name(public_name)
        else:
            raise PersistentStoreError("authority_unavailable")
        if access is None:
            raise PersistentStoreError("access_identity_not_found")
        return access

    async def _resolve_access(self, access_code: str) -> dict:
        try:
            if self.fleet_bridge is not None:
                return await self.fleet_bridge.resolve_access_code(access_code)
            if self.access_authority is None:
                raise PersistentStoreError("authority_unavailable")
            access = await self.access_authority.resolve_access_code(access_code)
            if access is None:
                raise PersistentStoreError("access_denied")
            return access
        except ValueError as exc:
            raise PersistentStoreError("access_denied") from exc

    async def _local_access_session(
        self,
        access: dict,
        *,
        require_active: bool = True,
        allow_stopping: bool = False,
        access_code_verified: bool = False,
    ):
        logical_agent_id = access["logical_agent_id"]
        if access["authority_node_id"] != self.lifecycle.authority_node_id:
            raise PersistentStoreError("authority_unavailable")
        session = await self.lifecycle.store.active_session_for_slot(logical_agent_id)
        if session is None:
            if require_active:
                raise PersistentStoreError("session_not_found")
            return None
        if session.state != "active":
            if not (allow_stopping and session.state == "stopping"):
                raise PersistentStoreError("session_stopping")
            return session
        try:
            await self.lifecycle.authorize_session(
                logical_agent_id,
                session.work_session_id,
                session.session_epoch,
                access_code_verified=access_code_verified,
            )
        except PersistentLifecycleError as exc:
            raise PersistentStoreError(exc.code, blockers=exc.blockers) from exc
        return session

    @staticmethod
    def _session_result(access: dict, session, *, access_code: str | None = None) -> dict:
        if isinstance(session, dict):
            session_ref = session["work_session_id"]
            session_epoch = session["session_epoch"]
            hard_expires_at = session["hard_expires_at"]
        else:
            session_ref = session.work_session_id
            session_epoch = session.session_epoch
            hard_expires_at = session.hard_expires_at
        result = {
            "ok": True,
            "mode": access["slot_kind"],
            "public_name": access["public_name"],
            "display_suffix": access.get("display_suffix"),
            "session_ref": session_ref,
            "session_epoch": session_epoch,
            "hard_expires_at": hard_expires_at,
        }
        if access_code is not None:
            result["access_code"] = access_code
        return result

    async def _local_session_start_resolved(self, access: dict) -> dict:
        logical_agent_id = access["logical_agent_id"]
        active = await self.lifecycle.store.active_session_for_slot(logical_agent_id)
        if active is not None and active.state in {"active", "stopping"}:
            raise PersistentStoreError(
                "session_already_active" if active.state == "active" else "session_stopping"
            )
        slot = await self.lifecycle.store.get_slot(logical_agent_id)
        if slot is None or slot.state == "deleted":
            raise PersistentStoreError("slot_not_found")
        selector = await self.lifecycle.store.active_selector(logical_agent_id)
        if selector is None:
            raise PersistentStoreError("selector_not_found")
        if slot.state not in {"armed", "suspended"}:
            raise PersistentStoreError("slot_not_armed")
        try:
            started = await self.lifecycle.session_start(
                selector["selector"], expected_revision=slot.slot_revision
            )
        except PersistentLifecycleError as exc:
            raise PersistentStoreError(exc.code, blockers=exc.blockers) from exc
        return self._session_result(access, started["work_session"])

    async def access_session_start(
        self, *, mode: str, access_code: str | None = None, display_name: str | None = None
    ) -> dict:
        if mode not in {"persistent", "legacy"}:
            return {"ok": False, "code": "invalid_mode", "error": "invalid_mode"}
        try:
            if mode == "persistent":
                if not access_code:
                    raise PersistentStoreError("access_code_required")
                access = await self._resolve_access(access_code)
                if access["slot_kind"] != "persistent":
                    raise PersistentStoreError("access_mode_mismatch")
                if access["authority_node_id"] != self.lifecycle.authority_node_id:
                    if self.fleet_bridge is None:
                        raise PersistentStoreError("authority_unavailable")
                    return await self.fleet_bridge.unified_session_call(
                        access["authority_node_id"], "start", {"access_code": access_code}
                    )
                return await self._local_session_start_resolved(access)

            if not self.service.legacy_agent_admission_enabled:
                raise PersistentStoreError("legacy_admission_disabled")
            created = await self.lifecycle.create_slot(
                (display_name or "Legacy").strip() or "Legacy"
            )
            logical_agent_id = created["slot"]["logical_agent_id"]
            try:
                access = await self._access_ensure(
                    logical_agent_id, display_suffix=display_name, slot_kind="legacy"
                )
                access_code = access.get("access_code")
                if not access_code:
                    raise PersistentStoreError("access_issue_failed")
                slot = await self.lifecycle.store.get_slot(logical_agent_id)
                armed = await self.lifecycle.play(
                    logical_agent_id, expected_revision=slot.slot_revision
                )
                selector = await self.lifecycle.store.active_selector(logical_agent_id)
                started = await self.lifecycle.session_start(
                    selector["selector"], expected_revision=armed["slot"]["slot_revision"]
                )
                return self._session_result(
                    access, started["work_session"], access_code=access_code
                )
            except Exception:
                slot = await self.lifecycle.store.get_slot(logical_agent_id)
                if slot is not None and slot.state not in {"active", "deleted"}:
                    try:
                        await self.lifecycle.delete(
                            logical_agent_id, expected_revision=slot.slot_revision
                        )
                    except Exception:
                        pass
                raise
        except PersistentStoreError as exc:
            return {
                "ok": False,
                "code": exc.code,
                "error": exc.code,
                "blockers": exc.blockers or None,
            }
        except PersistentLifecycleError as exc:
            return self._error(exc)

    async def access_identity(self, access_code: str) -> dict:
        try:
            access = await self._resolve_access(access_code)
            if access["authority_node_id"] != self.lifecycle.authority_node_id:
                if self.fleet_bridge is None:
                    raise PersistentStoreError("authority_unavailable")
                return await self.fleet_bridge.unified_session_call(
                    access["authority_node_id"], "status", {"access_code": access_code}
                )
            session = await self._local_access_session(access, access_code_verified=True)
            return {
                **self._session_result(access, session),
                "logical_agent_id": access["logical_agent_id"],
                "work_session_id": session.work_session_id,
            }
        except PersistentStoreError as exc:
            return {"ok": False, "code": exc.code, "error": exc.code}

    async def access_sender_identity(self, public_name: str) -> dict:
        try:
            access = await self._resolve_access_name(public_name)
            if access["authority_node_id"] != self.lifecycle.authority_node_id:
                if self.fleet_bridge is None:
                    raise PersistentStoreError("authority_unavailable")
                result = await self.fleet_bridge.unified_session_call(
                    access["authority_node_id"], "status-name", {"public_name": public_name}
                )
                return result
            session = await self._local_access_session(access)
            return {
                **self._session_result(access, session),
                "logical_agent_id": access["logical_agent_id"],
                "work_session_id": session.work_session_id,
            }
        except PersistentStoreError as exc:
            return {"ok": False, "code": exc.code, "error": exc.code}

    async def access_message(
        self,
        sender_public_name: str,
        *,
        text: str | None = None,
        target: str | None = None,
        message_hash: str | None = None,
        require_reply: bool = False,
        alert: bool = False,
        namespace: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        sender = await self.access_sender_identity(sender_public_name)
        if not sender.get("ok"):
            return sender
        sender_id = sender["logical_agent_id"]
        coordinator = self.service.agent_coordinator
        if coordinator is None:
            return {"ok": False, "code": "message_unavailable", "error": "message_unavailable"}
        require_reply = bool(require_reply or alert)
        if message_hash is not None:
            original = await coordinator.store.message_record(message_hash)
            if original is None:
                return {"ok": False, "code": "message_not_found", "error": "message_not_found"}
            recipient = await coordinator.store.recipient_record(message_hash, sender_id)
            if text is None:
                if recipient is not None:
                    await coordinator.store.acknowledge_message(message_hash, sender_id, utc_text())
                elif original["sender_agent_id"] != sender_id:
                    return {"ok": False, "code": "message_forbidden", "error": "message_forbidden"}
                receipts = await coordinator.store.message_receipts(message_hash)
                return {
                    "ok": True,
                    "sender": sender_public_name,
                    "message_hash": message_hash,
                    "read_by_count": sum(1 for item in receipts if item["read"]),
                    "replied_by_count": sum(1 for item in receipts if item["replied"]),
                }
            if recipient is None:
                return {"ok": False, "code": "message_forbidden", "error": "message_forbidden"}
            recipient_ids = [original["sender_agent_id"]]
            reply_hash = await coordinator._create_message(
                sender_id,
                text,
                None,
                recipient_ids,
                False,
                False,
                task_namespace=original.get("task_namespace"),
                task_id=original.get("task_id"),
            )
            await coordinator.store.mark_replied(message_hash, sender_id, reply_hash, utc_text())
            return {
                "ok": True,
                "sender": sender_public_name,
                "message_hash": reply_hash,
                "reply_to": message_hash,
            }

        if not text:
            return {"ok": False, "code": "message_text_required", "error": "message_text_required"}
        recipients: list[tuple[str, str]] = []
        if namespace is not None or task_id is not None:
            if not namespace or not task_id:
                return {"ok": False, "code": "invalid_task_target", "error": "invalid_task_target"}
            task = await self.task_store.get_task(namespace, task_id)
            if task is None:
                return {"ok": False, "code": "task_not_found", "error": "task_not_found"}
            for claim in await self.task_store.active_claims(namespace, task_id):
                rid = claim.get("owner_id") or claim["agent_id"]
                if rid == sender_id:
                    continue
                access = await self._access_get(rid)
                if access and access.get("status") == "active":
                    recipients.append((rid, access["public_name"]))
        elif target:
            access = await self._resolve_access_name(target)
            if access["logical_agent_id"] != sender_id:
                recipients.append((access["logical_agent_id"], access["public_name"]))
        else:
            return {
                "ok": False,
                "code": "message_target_required",
                "error": "message_target_required",
            }
        if not recipients:
            return {"ok": False, "code": "no_active_recipients", "error": "no_active_recipients"}
        allocated = await coordinator._create_message(
            sender_id,
            text,
            target,
            [rid for rid, _ in recipients],
            require_reply,
            alert,
            task_namespace=namespace,
            task_id=task_id,
        )
        if namespace and task_id:
            await self.task_store.add_event(
                namespace,
                task_id,
                "message",
                agent_id=sender_id,
                payload={
                    "message_hash": allocated,
                    "text": text,
                    "delivered_to": [name for _, name in recipients],
                },
            )
        return {
            "ok": True,
            "sender": sender_public_name,
            "message_hash": allocated,
            "delivered_to": [name for _, name in recipients],
            "namespace": namespace,
            "task_id": task_id,
        }

    async def access_observe_slots(self) -> dict:
        try:
            if self.fleet_bridge is not None:
                slots = await self.fleet_bridge.list_access_slots()
            elif self.access_authority is not None:
                slots = await self.access_authority.access_slots()
            else:
                raise PersistentStoreError("authority_unavailable")
            public = []
            for item in slots:
                view = {
                    "public_name": item["public_name"],
                    "mode": item["slot_kind"],
                    "display_suffix": item.get("display_suffix"),
                    "authority_node_id": item["authority_node_id"],
                    "access_generation": item["access_generation"],
                }
                if item["authority_node_id"] == self.lifecycle.authority_node_id:
                    session = await self.lifecycle.store.active_session_for_slot(
                        item["logical_agent_id"]
                    )
                    if session is not None:
                        view.update(
                            session_ref=session.work_session_id,
                            session_epoch=session.session_epoch,
                            session_state=session.state,
                            hard_expires_at=session.hard_expires_at,
                        )
                    else:
                        view["session_state"] = "inactive"
                public.append(view)
            return {"ok": True, "sessions": public}
        except PersistentStoreError as exc:
            return {"ok": False, "code": exc.code, "error": exc.code}

    async def _cleanup_legacy_access(self, access: dict) -> None:
        logical_agent_id = access["logical_agent_id"]
        await self.task_store.release_owner_claims(owner=ClaimOwner.logical_agent(logical_agent_id))
        slot = await self.lifecycle.store.get_slot(logical_agent_id)
        if slot is not None and slot.state != "deleted":
            await self.lifecycle.delete(logical_agent_id, expected_revision=slot.slot_revision)
        if self.fleet_bridge is not None:
            await self.fleet_bridge.retire_access_slot(logical_agent_id)
        elif self.access_authority is not None:
            await self.access_authority.retire_access_slot(logical_agent_id)

    async def access_session_stop(self, access_code: str, *, interrupt: bool = False) -> dict:
        try:
            access = await self._resolve_access(access_code)
            if access["authority_node_id"] != self.lifecycle.authority_node_id:
                if self.fleet_bridge is None:
                    raise PersistentStoreError("authority_unavailable")
                return await self.fleet_bridge.unified_session_call(
                    access["authority_node_id"],
                    "interrupt" if interrupt else "end",
                    {"access_code": access_code},
                )
            async with self.lifecycle.operation_guard(access["logical_agent_id"]):
                session = await self._local_access_session(
                    access, allow_stopping=True, access_code_verified=True
                )
                stop = self.lifecycle.session_interrupt if interrupt else self.lifecycle.session_end
                result = await stop(
                    access["logical_agent_id"],
                    session.work_session_id,
                    session.session_epoch,
                    access_code_verified=True,
                )
            if access["slot_kind"] == "legacy" and not result.get("stopping"):
                await self._cleanup_legacy_access(access)
            response = {
                "ok": True,
                "mode": access["slot_kind"],
                "public_name": access["public_name"],
                "session_ref": session.work_session_id,
                "interrupted": bool(interrupt),
                "stopping": bool(result.get("stopping")),
                "blockers": result.get("blockers") or [],
            }
            if not result.get("stopping"):
                response["hard_expires_at"] = result.get("hard_expires_at")
                response["remaining_d_seconds"] = result.get("remaining_d_seconds", 0)
                response["roaming_available"] = bool(result.get("roaming_available"))
                if result.get("roaming_message"):
                    response["roaming_message"] = result["roaming_message"]
            return response
        except PersistentStoreError as exc:
            return {"ok": False, "code": exc.code, "error": exc.code}
        except PersistentLifecycleError as exc:
            return self._error(exc)

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
        access_code: str | None = None,
        queue_id: int | None,
        task_scope: str,
    ):
        if not cmd:
            return {"ok": False, "code": "invalid_command", "error": "command is required"}
        try:
            async with self.lifecycle.operation_guard(logical_agent_id):
                session, permit = await self._execution_authority(
                    logical_agent_id,
                    work_session_id,
                    session_epoch,
                    scope="run",
                    access_code=access_code,
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
            return {"ok": False, "code": "invalid_command", "error": "command is required"}
        command = None
        try:
            async with self.lifecycle.operation_guard(logical_agent_id):
                session, permit = await self._execution_authority(
                    logical_agent_id,
                    work_session_id,
                    session_epoch,
                    scope="recovery",
                    access_code=access_code,
                )
                if permit is not None and self.fleet_bridge is not None:
                    self.fleet_bridge.ensure_permit_valid(permit)
                for _ in range(32):
                    try:
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
                            persistent_permit=permit.as_dict() if permit is not None else None,
                        )
                        break
                    except Exception as exc:
                        if exc.__class__.__name__ != "IntegrityError":
                            raise
                if command is None:
                    raise RuntimeError("unable to allocate unique command hash")
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
        except PersistentLifecycleError as exc:
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
        access_code: str | None = None,
        action: str,
        namespace: str,
        task_id: str | None = None,
        **kwargs,
    ):
        if self.task_coordinator is None:
            return {"ok": False, "code": "task_unavailable", "error": "task unavailable"}
        try:
            async with self.lifecycle.operation_guard(logical_agent_id):
                _session, permit = await self._execution_authority(
                    logical_agent_id,
                    work_session_id,
                    session_epoch,
                    scope="task",
                    access_code=access_code,
                )
                if permit is not None and self.fleet_bridge is not None:
                    self.fleet_bridge.ensure_permit_valid(permit)
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
