from __future__ import annotations

import asyncio
import secrets

from terminal_mcp.core.orchestration import normalize_preview, parse_utc, utc_now, utc_text
from terminal_mcp.core.persistent_admission import (
    PersistentAdmissionError,
    current_admission_context,
)
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.core.persistent_lifecycle import (
    PersistentLifecycleCoordinator,
    PersistentLifecycleError,
)
from terminal_mcp.core.public_errors import normalize_public_error
from terminal_mcp.storage.persistent_agents import PersistentStoreError

COMMAND_RUN_INLINE_BUDGET_SECONDS = 5.0
COMMAND_RUN_INLINE_POLL_SECONDS = 0.05
COMMAND_RUN_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


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
            except (PersistentLifecycleError, ValueError):
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
            slot_kind=slot_kind,
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
            started_at = session.get("started_at")
            session_state = session.get("state") or "active"
        else:
            session_ref = session.work_session_id
            session_epoch = session.session_epoch
            hard_expires_at = session.hard_expires_at
            started_at = session.started_at
            session_state = (
                session.state.value if hasattr(session.state, "value") else session.state
            )
        remaining = max(0, int((parse_utc(hard_expires_at) - utc_now()).total_seconds()))
        result = {
            "ok": True,
            "mode": access["slot_kind"],
            "public_name": access["public_name"],
            "display_suffix": access.get("display_suffix"),
            "session_ref": session_ref,
            "session_epoch": session_epoch,
            "session_state": session_state,
            "session_started_at": started_at,
            "hard_expires_at": hard_expires_at,
            "remaining_seconds": remaining,
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
        self,
        *,
        mode: str,
        access_code: str | None = None,
        display_name: str | None = None,
        _resolved_access: dict | None = None,
    ) -> dict:
        if mode not in {"persistent", "legacy"}:
            return {"ok": False, "code": "invalid_mode", "error": "invalid_mode"}
        try:
            if mode == "persistent":
                if not access_code:
                    raise PersistentStoreError("access_code_required")
                access = (
                    _resolved_access
                    if _resolved_access is not None
                    else await self._resolve_access(access_code)
                )
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

    async def access_active_statuses(self, accesses: list[dict]) -> list[dict]:
        """Resolve active local sessions for a bounded batch of access slots."""
        semaphore = asyncio.Semaphore(16)

        async def project(access: dict):
            logical_agent_id = str(access.get("logical_agent_id") or "")
            authority_node_id = str(access.get("authority_node_id") or "")
            if not logical_agent_id or authority_node_id != self.lifecycle.authority_node_id:
                return None
            async with semaphore:
                session = await self.lifecycle.store.active_session_for_slot(logical_agent_id)
                if session is None or session.state != "active":
                    return None
                try:
                    session = await self.lifecycle.store.assert_session_authority(
                        logical_agent_id,
                        session.work_session_id,
                        session.session_epoch,
                    )
                except PersistentStoreError:
                    return None
                if session.authority_node_id != self.lifecycle.authority_node_id:
                    return None
                last_seen = None
                provider_last_seen = getattr(self.lifecycle.store, "provider_last_seen", None)
                if callable(provider_last_seen):
                    last_seen = await provider_last_seen(logical_agent_id)
            return {
                "logical_agent_id": logical_agent_id,
                "session_state": session.state,
                "session_started_at": session.started_at,
                "hard_expires_at": session.hard_expires_at,
                "remaining_seconds": max(
                    0, int((parse_utc(session.hard_expires_at) - utc_now()).total_seconds())
                ),
                "last_active_at": last_seen,
            }

        projected = await asyncio.gather(*(project(access) for access in accesses))
        return [item for item in projected if item is not None]

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
            result = {
                **self._session_result(access, session),
                "logical_agent_id": access["logical_agent_id"],
                "work_session_id": session.work_session_id,
            }
            seen_values = []
            last_seen = getattr(self.lifecycle.store, "provider_last_seen", None)
            if callable(last_seen):
                value = await last_seen(access["logical_agent_id"])
                if value:
                    seen_values.append(value)
            authority_seen = getattr(self.access_authority, "provider_last_seen", None)
            if callable(authority_seen):
                value = await authority_seen(access["logical_agent_id"])
                if value:
                    seen_values.append(value)
            result["last_active_at"] = max(seen_values) if seen_values else None
            return result
        except PersistentStoreError as exc:
            return {"ok": False, "code": exc.code, "error": exc.code}

    async def access_message(
        self,
        sender_public_name: str,
        *,
        access_code: str | None = None,
        text: str | None = None,
        target: str | None = None,
        message_hash: str | None = None,
        require_reply: bool = False,
        alert: bool = False,
        mode: str | None = None,
        show_all: bool = False,
        limit: int = 50,
        offset: int = 0,
        surface_limit: int | None = None,
        namespace: str | None = None,
        task_id: str | None = None,
        _resolved_identity: dict | None = None,
    ) -> dict:
        normalized_mode = "notify"
        if mode is not None:
            normalized_mode = str(mode).strip().lower()
            if normalized_mode not in {"notify", "ack", "alert"}:
                return {
                    "ok": False,
                    "code": "invalid_message_mode",
                    "error": "invalid_message_mode",
                }
            require_reply = normalized_mode in {"ack", "alert"}
            alert = normalized_mode == "alert"
        elif alert or require_reply:
            normalized_mode = "alert"
            require_reply = True
            alert = True
        # Managed callers already carry a server-authorized session identity.
        # Access-code omission must not downgrade their Fleet routing to local.
        if access_code is None and _resolved_identity is None:
            return await self._access_message_legacy(
                sender_public_name,
                text=text,
                target=target,
                message_hash=message_hash,
                require_reply=require_reply,
                alert=alert,
                mode=normalized_mode,
                show_all=show_all,
                limit=limit,
                offset=offset,
                surface_limit=surface_limit,
                namespace=namespace,
                task_id=task_id,
                sender_identity=_resolved_identity,
            )
        sender = (
            _resolved_identity
            if _resolved_identity is not None
            else await self.access_identity(access_code)
        )
        if not sender.get("ok"):
            return sender
        if (
            sender_public_name
            and sender_public_name.casefold() != str(sender["public_name"]).casefold()
        ):
            return {"ok": False, "code": "sender_not_authorized", "error": "sender_not_authorized"}
        if self.fleet_bridge is None:
            return await self._access_message_legacy(
                str(sender["public_name"]),
                sender_identity=sender,
                text=text,
                target=target,
                message_hash=message_hash,
                require_reply=require_reply,
                alert=alert,
                mode=normalized_mode,
                show_all=show_all,
                limit=limit,
                offset=offset,
                surface_limit=surface_limit,
                namespace=namespace,
                task_id=task_id,
            )
        return await self._fleet_access_message(
            sender,
            access_code=access_code,
            text=text,
            target=target,
            message_hash=message_hash,
            require_reply=require_reply,
            alert=alert,
            show_all=show_all,
            limit=limit,
            offset=offset,
            surface_limit=surface_limit,
            namespace=namespace,
            task_id=task_id,
        )

    async def _persistent_message_entry(self, item: dict) -> dict:
        sender_id = str(item.get("sender_agent_id") or "")
        sender_access = await self._access_get(sender_id) if sender_id else None
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
                sender_access.get("public_name") if sender_access is not None else sender_id
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

    @staticmethod
    def _surface_message_views(rows: list[dict], *, seen_at: str) -> None:
        for item in rows:
            item["seen_count"] = int(item.get("seen_count") or 0) + 1
            item["first_seen_at"] = item.get("first_seen_at") or seen_at
            item["last_seen_at"] = seen_at
            alert = bool(item.get("alert"))
            require_reply = bool(item.get("require_reply"))
            declared_mode = item.get("delivery_mode") or item.get("mode")
            notify = declared_mode == "notify" or (
                declared_mode not in {"ack", "alert"} and not require_reply and not alert
            )
            if notify:
                item["read_at"] = item.get("read_at") or seen_at

    async def surface_message_page(
        self,
        sender_public_name: str,
        *,
        access_code: str | None,
        message_hashes: list[str],
        _resolved_identity: dict | None = None,
    ) -> dict:
        refs = list(dict.fromkeys(str(ref) for ref in message_hashes if ref))
        if not refs:
            return {"ok": True, "seen_at": utc_text(), "surfaced": []}
        if access_code is None:
            sender = (
                _resolved_identity
                if _resolved_identity is not None
                else await self.access_sender_identity(sender_public_name)
            )
        else:
            sender = (
                _resolved_identity
                if _resolved_identity is not None
                else await self.access_identity(access_code)
            )
            if (
                sender.get("ok")
                and sender_public_name
                and (sender_public_name.casefold() != str(sender["public_name"]).casefold())
            ):
                return {
                    "ok": False,
                    "code": "sender_not_authorized",
                    "error": "sender_not_authorized",
                }
        if not sender.get("ok"):
            return sender
        sender_id = str(sender["logical_agent_id"])
        seen_at = utc_text()
        if access_code is None or self.fleet_bridge is None:
            coordinator = self.service.agent_coordinator
            if coordinator is None:
                return {"ok": False, "code": "message_unavailable", "error": "message_unavailable"}
            await coordinator.store.mark_messages_seen(sender_id, refs, seen_at)
            return {"ok": True, "seen_at": seen_at, "surfaced": refs}
        work_session_id = str(sender["work_session_id"])
        session_epoch = int(sender["session_epoch"])
        try:
            _session, permit = await self._execution_authority(
                sender_id, work_session_id, session_epoch, scope="message", access_code=access_code
            )
            if permit is not None:
                self.fleet_bridge.ensure_permit_valid(permit)
            inbox = await self.fleet_bridge.inbox_obligations(
                sender_id, work_session_id=work_session_id, session_epoch=session_epoch
            )
            wanted = set(refs)
            obligations = [item for item in inbox if str(item.get("message_ref") or "") in wanted]
            if obligations:
                await self.fleet_bridge.surface_obligations(
                    logical_agent_id=sender_id,
                    work_session_id=work_session_id,
                    session_epoch=session_epoch,
                    obligations=obligations,
                    retention_calls=5,
                )
        except (PersistentLifecycleError, PersistentStoreError) as exc:
            code = exc.code
            return {"ok": False, "code": code, "error": code}
        return {
            "ok": True,
            "seen_at": seen_at,
            "surfaced": [
                str(item.get("message_ref")) for item in obligations if item.get("message_ref")
            ],
        }

    async def message_state(
        self,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        surface: bool = False,
    ) -> dict:
        if self.fleet_bridge is None:
            coordinator = self.service.agent_coordinator
            if coordinator is None:
                return {
                    "messages": [],
                    "pending_messages": [],
                    "ack_required_pending": False,
                    "alert_pending": False,
                }
            obligations = await coordinator.store.message_obligations(logical_agent_id)
            visible = list(obligations)
            if surface and visible:
                await coordinator.store.mark_messages_seen(
                    logical_agent_id,
                    [item["message_hash"] for item in visible],
                    utc_text(),
                )
            refreshed = await coordinator.store.message_obligations(logical_agent_id)
            journal = await coordinator.store.message_journal(logical_agent_id, limit=500)
            current = {item["message_hash"]: item for item in journal}
            messages = [
                await self._persistent_message_entry(current.get(item["message_hash"], item))
                for item in visible
            ]
            return {
                "messages": messages,
                "pending_messages": messages,
                "ack_required_pending": any(
                    item["read_at"] is None
                    or (
                        item.get("delivery_mode") == "legacy"
                        and item["require_reply"]
                        and item["replied_at"] is None
                    )
                    for item in refreshed
                ),
                "alert_pending": any(
                    (item["alert"] or item.get("delivery_mode") == "alert")
                    and item["replied_at"] is None
                    for item in refreshed
                ),
            }
        obligations = await self.fleet_bridge.inbox_obligations(
            logical_agent_id,
            work_session_id=work_session_id,
            session_epoch=session_epoch,
        )
        visible = list(obligations)
        if surface and visible:
            await self.fleet_bridge.surface_obligations(
                logical_agent_id=logical_agent_id,
                work_session_id=work_session_id,
                session_epoch=session_epoch,
                obligations=visible,
                retention_calls=5,
            )
        messages = []
        for item in visible:
            view = dict(item)
            if surface:
                view["seen_count"] = int(view.get("seen_count") or 0) + 1
                view["first_seen_at"] = view.get("first_seen_at") or utc_text()
                view["last_seen_at"] = utc_text()
                if not bool(view.get("require_reply")) and not bool(view.get("alert")):
                    view["read_at"] = view.get("read_at") or view["last_seen_at"]
            messages.append(await self._persistent_message_entry(view))
        return {
            "messages": messages,
            "pending_messages": messages,
            "ack_required_pending": any(
                bool(item.get("require_reply")) and not bool(item.get("alert"))
                for item in obligations
            ),
            "alert_pending": any(bool(item.get("alert")) for item in obligations),
        }

    async def _fleet_access_message(
        self,
        sender: dict,
        *,
        access_code: str | None,
        text: str | None,
        target: str | None,
        message_hash: str | None,
        require_reply: bool,
        alert: bool,
        show_all: bool,
        limit: int,
        offset: int,
        surface_limit: int | None,
        namespace: str | None,
        task_id: str | None,
    ) -> dict:
        sender_id = str(sender["logical_agent_id"])
        sender_name = str(sender["public_name"])
        work_session_id = str(sender["work_session_id"])
        session_epoch = int(sender["session_epoch"])
        try:
            _session, permit = await self._execution_authority(
                sender_id,
                work_session_id,
                session_epoch,
                scope="message",
                access_code=access_code,
            )
            if permit is not None:
                self.fleet_bridge.ensure_permit_valid(permit)
        except PersistentLifecycleError as exc:
            return self._error(exc)

        try:
            inbox = await self.fleet_bridge.inbox_obligations(
                sender_id,
                work_session_id=work_session_id,
                session_epoch=session_epoch,
            )
        except PersistentStoreError as exc:
            return {"ok": False, "code": exc.code, "error": exc.code}
        if message_hash is not None:
            original = next(
                (item for item in inbox if item.get("message_ref") == message_hash),
                None,
            )
            if original is None:
                try:
                    history = await self.fleet_bridge.message_inbox(
                        sender_id,
                        work_session_id=work_session_id,
                        session_epoch=session_epoch,
                        show_all=True,
                        recent_seconds=300,
                        limit=500,
                    )
                except PersistentStoreError as exc:
                    return {"ok": False, "code": exc.code, "error": exc.code}
                original = next(
                    (item for item in history if item.get("message_ref") == message_hash),
                    None,
                )
            if original is None:
                # Preserve pre-Fleet/local message hashes during the cutover.
                return await self._access_message_legacy(
                    sender_name,
                    text=text,
                    target=target,
                    message_hash=message_hash,
                    require_reply=require_reply,
                    alert=alert,
                    namespace=namespace,
                    task_id=task_id,
                )
            reply_ref = None
            if text is not None:
                recipient_id = str(original.get("sender_agent_id") or "")
                if not recipient_id or recipient_id == sender_id:
                    return {
                        "ok": False,
                        "code": "no_active_recipients",
                        "error": "no_active_recipients",
                    }
                try:
                    delivered = await self.fleet_bridge.deliver_message(
                        logical_agent_id=recipient_id,
                        sender_agent_id=sender_id,
                        text=text,
                        require_reply=False,
                        alert=False,
                    )
                except PersistentStoreError as exc:
                    return {"ok": False, "code": exc.code, "error": exc.code}
                reply_ref = str(delivered["message_ref"])
            try:
                await self.fleet_bridge.acknowledge_obligation(
                    obligation=original,
                    logical_agent_id=sender_id,
                    work_session_id=work_session_id,
                    session_epoch=session_epoch,
                    replied_at=utc_text() if reply_ref is not None else None,
                    reply_message_ref=reply_ref,
                )
            except PersistentStoreError as exc:
                return {"ok": False, "code": exc.code, "error": exc.code}
            return {
                "ok": True,
                "sender": sender_name,
                "message_hash": reply_ref or message_hash,
                "reply_to": message_hash if reply_ref else None,
            }

        if text is None:
            try:
                rows = await self.fleet_bridge.message_inbox(
                    sender_id,
                    work_session_id=work_session_id,
                    session_epoch=session_epoch,
                    show_all=show_all,
                    recent_seconds=300,
                    limit=max(1, min(int(limit), 500)),
                    offset=max(0, int(offset)),
                )
                if not show_all and rows:
                    count = (
                        len(rows)
                        if surface_limit is None
                        else max(0, min(len(rows), int(surface_limit)))
                    )
                    surfaced_rows = rows[:count]
                    if surfaced_rows:
                        seen_at = utc_text()
                        await self.fleet_bridge.surface_obligations(
                            logical_agent_id=sender_id,
                            work_session_id=work_session_id,
                            session_epoch=session_epoch,
                            obligations=surfaced_rows,
                            retention_calls=5,
                        )
                        self._surface_message_views(surfaced_rows, seen_at=seen_at)
            except PersistentStoreError as exc:
                return {"ok": False, "code": exc.code, "error": exc.code}
            visible = [await self._persistent_message_entry(item) for item in rows]
            return {
                "ok": True,
                "sender": sender_name,
                "messages": visible,
                "inbox": visible,
                "show_all": bool(show_all),
            }

        recipients: list[tuple[str, str]] = []
        if target and target.casefold() == "broadcast":
            target = None
        if namespace is not None or task_id is not None:
            if not namespace or not task_id:
                return {"ok": False, "code": "invalid_task_target", "error": "invalid_task_target"}
            task = await self.task_store.get_task(namespace, task_id)
            if task is None:
                return {"ok": False, "code": "task_not_found", "error": "task_not_found"}
            for claim in await self.task_store.active_claims(namespace, task_id):
                rid = str(claim.get("owner_id") or claim["agent_id"])
                if rid == sender_id:
                    continue
                access = await self._access_get(rid)
                if access is not None and access.get("status") == "active":
                    recipients.append((rid, str(access["public_name"])))
        elif target and target.casefold() != "broadcast":
            try:
                access = await self._resolve_access_name(target)
            except PersistentStoreError as exc:
                code = (
                    "recipient_not_found" if exc.code == "access_identity_not_found" else exc.code
                )
                return {"ok": False, "code": code, "error": code}
            if access["logical_agent_id"] == sender_id:
                return {
                    "ok": False,
                    "code": "no_active_recipients",
                    "error": "no_active_recipients",
                }
            recipients.append((str(access["logical_agent_id"]), str(access["public_name"])))
        else:
            # Persistent implicit broadcast is Fleet-scoped, never attachment-local.
            try:
                slots = await self.fleet_bridge.list_access_slots()
            except PersistentStoreError as exc:
                return {"ok": False, "code": exc.code, "error": exc.code}
            for access in slots:
                rid = str(access.get("logical_agent_id") or "")
                if not rid or rid == sender_id or access.get("status") != "active":
                    continue
                recipients.append((rid, str(access["public_name"])))

        if not recipients:
            return {"ok": False, "code": "no_active_recipients", "error": "no_active_recipients"}

        delivered_to: list[str] = []
        message_hashes: dict[str, str] = {}
        authority_failure = False
        inactive_count = 0
        for rid, name in recipients:
            try:
                delivered = await self.fleet_bridge.deliver_message(
                    logical_agent_id=rid,
                    sender_agent_id=sender_id,
                    text=text,
                    require_reply=require_reply,
                    alert=alert,
                )
            except PersistentStoreError as exc:
                if exc.code == "recipient_not_active":
                    inactive_count += 1
                    continue
                if exc.code in {"authority_unavailable", "wrong_authority", "recovery_required"}:
                    authority_failure = True
                    continue
                if len(recipients) == 1:
                    return {"ok": False, "code": exc.code, "error": exc.code}
                continue
            delivered_to.append(name)
            message_hashes[name] = str(delivered["message_ref"])

        if not delivered_to:
            if authority_failure:
                return {
                    "ok": False,
                    "code": "authority_unavailable",
                    "error": "authority_unavailable",
                }
            code = (
                "recipient_not_active"
                if len(recipients) == 1 and inactive_count
                else "no_active_recipients"
            )
            return {"ok": False, "code": code, "error": code}

        response = {
            "ok": True,
            "sender": sender_name,
            "delivered_to": delivered_to,
            "scope": "fleet"
            if not (namespace and task_id) and (not target or target.casefold() == "broadcast")
            else "direct",
            "namespace": namespace,
            "task_id": task_id,
        }
        if len(message_hashes) == 1:
            response["message_hash"] = next(iter(message_hashes.values()))
        else:
            response["message_hashes"] = message_hashes
        return response

    async def _access_message_legacy(
        self,
        sender_public_name: str,
        *,
        sender_identity: dict | None = None,
        text: str | None = None,
        target: str | None = None,
        message_hash: str | None = None,
        require_reply: bool = False,
        alert: bool = False,
        mode: str = "notify",
        show_all: bool = False,
        limit: int = 50,
        offset: int = 0,
        surface_limit: int | None = None,
        namespace: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        sender = sender_identity or await self.access_sender_identity(sender_public_name)
        if not sender.get("ok"):
            return sender
        sender_id = sender["logical_agent_id"]
        coordinator = self.service.agent_coordinator
        if coordinator is None:
            return {"ok": False, "code": "message_unavailable", "error": "message_unavailable"}
        require_reply = bool(require_reply or alert)
        mode = mode if mode in {"notify", "ack", "alert"} else "notify"
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
            page_limit = max(1, min(int(limit), 500))
            page_offset = max(0, int(offset))
            if show_all:
                rows = await coordinator.store.message_journal(
                    sender_id,
                    limit=page_limit,
                    offset=page_offset,
                )
            else:
                open_hashes = {
                    item["message_hash"]
                    for item in await coordinator.store.message_obligations(sender_id)
                }
                rows = []
                scan_offset = 0
                skipped = 0
                scan_limit = max(50, min(500, page_limit * 2))
                while len(rows) < page_limit:
                    batch = await coordinator.store.message_journal(
                        sender_id,
                        limit=scan_limit,
                        offset=scan_offset,
                    )
                    if not batch:
                        break
                    for item in batch:
                        if item["message_hash"] not in open_hashes:
                            continue
                        if skipped < page_offset:
                            skipped += 1
                            continue
                        rows.append(item)
                        if len(rows) >= page_limit:
                            break
                    scan_offset += len(batch)
                    if len(batch) < scan_limit:
                        break
            if not show_all and rows:
                count = (
                    len(rows)
                    if surface_limit is None
                    else max(0, min(len(rows), int(surface_limit)))
                )
                surfaced_rows = rows[:count]
                if surfaced_rows:
                    seen_at = utc_text()
                    await coordinator.store.mark_messages_seen(
                        sender_id,
                        [item["message_hash"] for item in surfaced_rows],
                        seen_at,
                    )
                    self._surface_message_views(surfaced_rows, seen_at=seen_at)
            messages = [await self._persistent_message_entry(item) for item in rows]
            return {
                "ok": True,
                "sender": sender_public_name,
                "messages": messages,
                "inbox": messages,
                "show_all": bool(show_all),
                "scope": "local",
            }
        recipients: list[tuple[str, str]] = []
        if target and target.casefold() == "broadcast":
            target = None
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
            try:
                access = await self._resolve_access_name(target)
            except PersistentStoreError as exc:
                code = (
                    "recipient_not_found" if exc.code == "access_identity_not_found" else exc.code
                )
                return {"ok": False, "code": code, "error": code}
            if access["logical_agent_id"] == sender_id:
                return {
                    "ok": False,
                    "code": "no_active_recipients",
                    "error": "no_active_recipients",
                }
            session = await self.lifecycle.store.active_session_for_slot(access["logical_agent_id"])
            if (
                session is None
                or session.state != "active"
                or utc_now() >= parse_utc(session.hard_expires_at)
            ):
                return {
                    "ok": False,
                    "code": "recipient_not_active",
                    "error": "recipient_not_active",
                }
            recipients.append((access["logical_agent_id"], access["public_name"]))
        else:
            if self.access_authority is None:
                return {
                    "ok": False,
                    "code": "no_active_recipients",
                    "error": "no_active_recipients",
                }
            for access in await self.access_authority.access_slots():
                rid = str(access.get("logical_agent_id") or "")
                if not rid or rid == sender_id or access.get("status") != "active":
                    continue
                session = await self.lifecycle.store.active_session_for_slot(rid)
                if (
                    session is None
                    or session.state != "active"
                    or utc_now() >= parse_utc(session.hard_expires_at)
                ):
                    continue
                recipients.append((rid, str(access["public_name"])))
        if not recipients:
            return {"ok": False, "code": "no_active_recipients", "error": "no_active_recipients"}
        allocated = await coordinator._create_message(
            sender_id,
            text,
            target,
            [rid for rid, _ in recipients],
            require_reply,
            alert,
            delivery_mode=mode,
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
            "scope": "direct" if target or (namespace and task_id) else "local",
            "mode": mode,
            "namespace": namespace,
            "task_id": task_id,
        }

    async def access_observe_slots(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict:
        try:
            page_limit = None if limit is None else max(1, int(limit))
            active_offset = max(0, int(offset))
            raw_offset = 0
            matched = 0
            sessions: list[dict] = []
            batch_size = 64

            async def statuses_for_batch(slots: list[dict]) -> dict[str, dict]:
                groups: dict[str, list[dict]] = {}
                for slot in slots:
                    authority = str(slot.get("authority_node_id") or "")
                    logical_agent_id = str(slot.get("logical_agent_id") or "")
                    if authority and logical_agent_id:
                        groups.setdefault(authority, []).append(
                            {
                                "logical_agent_id": logical_agent_id,
                                "authority_node_id": authority,
                            }
                        )

                async def resolve(authority: str, items: list[dict]) -> list[dict]:
                    if authority == self.lifecycle.authority_node_id:
                        return await self.access_active_statuses(items)
                    if self.fleet_bridge is None:
                        return []
                    try:
                        result = await self.fleet_bridge.unified_session_call(
                            authority, "status-batch", {"access": items}
                        )
                    except PersistentStoreError:
                        return []
                    return list(result.get("sessions") or [])

                resolved = await asyncio.gather(
                    *(resolve(authority, items) for authority, items in groups.items())
                )
                return {
                    str(item.get("logical_agent_id")): item
                    for group in resolved
                    for item in group
                    if item.get("logical_agent_id")
                }

            while page_limit is None or len(sessions) < page_limit:
                if self.fleet_bridge is not None:
                    slots = await self.fleet_bridge.list_access_slots(
                        limit=batch_size, offset=raw_offset
                    )
                elif self.access_authority is not None:
                    slots = await self.access_authority.access_slots(
                        limit=batch_size, offset=raw_offset
                    )
                else:
                    raise PersistentStoreError("authority_unavailable")
                if not slots:
                    break
                statuses = await statuses_for_batch(slots)
                for slot in slots:
                    status = statuses.get(str(slot.get("logical_agent_id") or ""))
                    if status is None:
                        continue
                    if matched < active_offset:
                        matched += 1
                        continue
                    sessions.append(
                        {
                            "public_name": slot["public_name"],
                            "session_state": status.get("session_state", "active"),
                            "session_started_at": status.get("session_started_at"),
                            "hard_expires_at": status.get("hard_expires_at"),
                            "remaining_seconds": status.get("remaining_seconds", 0),
                            "last_active_at": status.get("last_active_at"),
                        }
                    )
                    matched += 1
                    if page_limit is not None and len(sessions) >= page_limit:
                        break
                raw_offset += len(slots)
                if len(slots) < batch_size:
                    break
            return {"ok": True, "sessions": sessions}
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

    async def access_session_stop(
        self,
        access_code: str,
        *,
        interrupt: bool = False,
        _resolved_access: dict | None = None,
    ) -> dict:
        """Compatibility entry; canonical callers supply a verified access resolution."""
        try:
            access = (
                _resolved_access
                if _resolved_access is not None
                else await self._resolve_access(access_code)
            )
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
                return await self._stop_resolved_session(access, session, interrupt=interrupt)
        except PersistentStoreError as exc:
            return {"ok": False, "code": exc.code, "error": exc.code}
        except PersistentLifecycleError as exc:
            return self._error(exc)

    async def _stop_resolved_session(self, access, session, *, interrupt=False) -> dict:
        """Apply a session stop resolved under the caller-held operation guard."""
        stop = self.lifecycle.session_interrupt if interrupt else self.lifecycle.session_end
        result = await stop(
            access["logical_agent_id"],
            session.work_session_id,
            session.session_epoch,
            access_code_verified=True,
        )
        if access["slot_kind"] == "legacy" and (not result.get("stopping")):
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
            slots = result.get("slots") or []
            # Access identity may be resolved through Fleet authority. Serial resolution
            # made a 49-slot Tokyo read exceed the Console request deadline. Bound the
            # fan-out while preserving slot order and the existing response shape.
            limit = asyncio.Semaphore(8)

            async def enrich_access(item):
                async with limit:
                    logical_agent_id = item["slot"]["logical_agent_id"]
                    item["access"] = await self._access_get(logical_agent_id)
                    return item

            if slots:
                tasks = [asyncio.create_task(enrich_access(item)) for item in slots]
                try:
                    await asyncio.gather(*tasks)
                except BaseException:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
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
                if self.fleet_bridge is not None:
                    await self.fleet_bridge.publish_authority(
                        logical_agent_id,
                        result["slot"]["authority_node_id"],
                        int(result["slot"]["authority_epoch"]),
                    )
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
        except PersistentLifecycleError as exc:
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

        return await self._idempotent(
            logical_agent_id,
            f"command.{action}",
            idempotency_key,
            {
                "command": cmd,
                "queue_id": queue_id,
                "task_scope": task_scope,
                "work_session_id": work_session_id,
                "session_epoch": session_epoch,
            },
            launch_once,
            preserve_uncertain=True,
        )

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
        loop = asyncio.get_running_loop()
        inline_deadline = loop.time() + COMMAND_RUN_INLINE_BUDGET_SECONDS
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
            current = await self.repo.get(command.cmd_hash)
            queue_position = await self.repo.queue_position(command.cmd_hash)
            backlogged = False
            if current is not None and current.status == "queued":
                snapshot = await self.repo.queue_snapshot(self.terminal.queue_workers)
                queue_state = next(
                    (item for item in snapshot if item.get("queue_id") == command.queue_id),
                    None,
                )
                running_hash = queue_state.get("running") if queue_state is not None else None
                backlogged = bool(
                    (running_hash is not None and running_hash != command.cmd_hash)
                    or (queue_position is not None and queue_position > 1)
                )

            while (
                not backlogged
                and current is not None
                and current.status not in COMMAND_RUN_TERMINAL_STATES
                and loop.time() < inline_deadline
            ):
                remaining = inline_deadline - loop.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(COMMAND_RUN_INLINE_POLL_SECONDS, remaining))
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
                "hard_expires_at": (
                    session.hard_expires_at if session is not None else permit.hard_expires_at
                ),
                "authority_node_id": (
                    session.authority_node_id if session is not None else permit.authority_node_id
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
        idempotency_key: str | None = None,
        session_scoped_claim: bool = False,
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

                claim_lease = None
                if action == "claim":
                    managed = await self.lifecycle.store.has_managed_window(logical_agent_id)
                    access = await self._access_get(logical_agent_id)
                    if (
                        session_scoped_claim
                        or managed
                        or (access or {}).get("slot_kind") == "legacy"
                    ):
                        authority = _session or permit
                        claim_lease = {
                            "work_session_id": work_session_id,
                            "session_epoch": session_epoch,
                            "hard_expires_at": authority.hard_expires_at,
                        }

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
                    if result.get("ok") and actual_task:
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
                        if action == "claim":
                            try:
                                snapshot = await self.task_coordinator.list(
                                    namespace=actual_task["namespace"],
                                    task_id=actual_task["task_id"],
                                    snapshot=True,
                                )
                                if snapshot.get("ok") and snapshot.get("task") is not None:
                                    result["task"] = snapshot["task"]
                            except Exception:
                                pass
                    return result

                if idempotency_key:
                    return await self._idempotent(
                        logical_agent_id,
                        f"task.{action}",
                        idempotency_key,
                        {
                            "namespace": namespace,
                            "task_id": task_id,
                            "action": action,
                            "kwargs": kwargs,
                        },
                        mutate_once,
                    )
                return await mutate_once()
        except PersistentLifecycleError as exc:
            return self._error(exc)
        except PersistentStoreError as exc:
            return {"ok": False, "code": exc.code, "error": exc.code}

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
