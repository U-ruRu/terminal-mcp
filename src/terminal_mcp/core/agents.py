from __future__ import annotations

import asyncio
import secrets
from datetime import timedelta
from sqlite3 import IntegrityError

from terminal_mcp.core.agent_policy import AgentPolicy
from terminal_mcp.core.orchestration import (
    NATO_WORDS,
    find_scope_overlaps,
    generate_agent_id,
    is_agent_id,
    live_task_claims,
    parse_utc,
    public_agent_name,
    public_session_ref,
    relative_time,
    session_expiry_reason,
    short_time,
    utc_now,
    utc_text,
    validate_message_routing,
)


class AgentCoordinator:
    ALERT_BLOCKED_TOOLS = {"run", "read", "coordinate", "agents", "agent_start", "task"}
    SESSION_ALERT_SENDER = "system-session"

    def __init__(
        self,
        store,
        metrics=None,
        policy: AgentPolicy | None = None,
        task_store=None,
        foreign_session_resolver=None,
        local_instance_id: str | None = None,
    ):
        self.store = store
        self.metrics = metrics
        self.policy = policy or AgentPolicy()
        self.task_store = task_store
        self.foreign_session_resolver = foreign_session_resolver
        self.local_instance_id = local_instance_id
        # Mutable aliases preserve the existing testing/diagnostic surface.
        self.ttl_seconds = self.policy.idle_ttl_seconds
        self.task_context_ttl_seconds = self.policy.intent_ttl_seconds
        self.max_session_seconds = self.policy.max_session_seconds
        self.session_warning_after_seconds = self.policy.session_warning_after_seconds
        self._start_lock = asyncio.Lock()

    @property
    def task_lease_seconds(self):
        """Compatibility alias; this timer governs task-context freshness, not claim ownership."""
        return self.task_context_ttl_seconds

    @task_lease_seconds.setter
    def task_lease_seconds(self, value):
        self.task_context_ttl_seconds = value

    async def _reserved_public_names(self, now_dt):
        active_cutoff = utc_text(now_dt - timedelta(seconds=self.ttl_seconds))
        active = await self.store.active(active_cutoff, len(NATO_WORDS))
        reserved = {public_agent_name(item["agent_id"]) for item in active}

        recent_cutoff = utc_text(
            now_dt - timedelta(seconds=self.policy.public_name_reservation_seconds)
        )
        recent = await self.store.history_sessions(recent_cutoff, len(NATO_WORDS) * 4)
        reserved.update(
            public_agent_name(item["agent_id"])
            for item in recent
            if item["state"] != "active"
            and item.get("ended_at")
            and parse_utc(item["ended_at"]) >= parse_utc(recent_cutoff)
        )
        proposals = await self.store.recent_proposals(recent_cutoff)
        reserved.update(public_agent_name(item["agent_id"]) for item in proposals)
        return reserved

    @staticmethod
    def _missing_registration_field(task_summary, intent, details):
        if not task_summary:
            return "agent_start.task_summary: required for registration"
        if not intent:
            return "agent_start.intent: required for registration"
        if not details:
            return "agent_start.details: required for registration"
        return None

    def _foreign_unavailable(self, agent_id, detail=None):
        return {
            "ok": False,
            "agent_name": public_agent_name(agent_id),
            "foreign_identity_unavailable": True,
            "retryable": True,
            "error": "agent.identity: trusted fleet identity is temporarily unavailable",
            "detail": detail
            or (
                "Retry after peer recovery or return to the authoritative Terminal MCP. "
                "This server will not mint a new hard session for an unknown full agent_id."
            ),
        }

    def _foreign_terminal(self, agent_id, resolution, *, reason=None):
        return {
            "ok": False,
            "agent_name": public_agent_name(agent_id),
            "return_to_chat": True,
            "session_status": resolution.get("state") or "forced",
            "session_started_at": resolution.get("session_started_at"),
            "session_end_reason": reason
            or resolution.get("end_reason")
            or "max_session_duration",
            "error": "agent session has ended; return to chat before starting new work",
        }

    async def _resolve_foreign(self, agent_id):
        if self.foreign_session_resolver is None:
            return None
        try:
            return await self.foreign_session_resolver(agent_id)
        except Exception:
            return None

    async def _attach_foreign(self, agent_id, current=None):
        resolution = await self._resolve_foreign(agent_id)
        if resolution is None:
            return None, self._foreign_unavailable(agent_id)
        source = resolution.get("source_instance_id")
        if not source or source == self.local_instance_id:
            return None, self._foreign_unavailable(
                agent_id, "Fleet identity source is not foreign."
            )
        now_dt = utc_now()
        expires_at = resolution.get("expires_at")
        if not expires_at:
            return None, self._foreign_unavailable(agent_id, "Fleet identity has no hard expiry.")
        if resolution.get("state") != "active":
            return None, self._foreign_terminal(agent_id, resolution)
        if now_dt >= parse_utc(expires_at):
            return None, self._foreign_terminal(
                agent_id, resolution, reason="max_session_duration"
            )
        task_summary = resolution.get("task_summary")
        intent = resolution.get("intent")
        details = list(resolution.get("details") or [])
        work_scope = list(resolution.get("work_scope") or [])
        current_step = int(resolution.get("current_step") or 1)
        if not task_summary or not intent or not details:
            return None, self._foreign_unavailable(
                agent_id, "Fleet identity does not contain a signed shared-session plan."
            )
        current_step = min(max(current_step, 1), len(details))
        now = utc_text(now_dt)
        if current is not None:
            can_reactivate = (
                current.get("source_instance_id") == source
                and current.get("state") == "forced"
                and current.get("end_reason") == "idle_timeout"
            )
            if not can_reactivate:
                return None, self.expired_result(agent_id, current)
            changed = await self.store.reactivate_foreign_session(
                agent_id,
                task_summary,
                intent,
                work_scope,
                details,
                current_step,
                now,
                registered_at=resolution["session_started_at"],
                source_instance_id=source,
                global_expires_at=expires_at,
            )
            if not changed:
                return None, self._foreign_unavailable(
                    agent_id, "Local foreign-session attachment changed concurrently."
                )
        else:
            try:
                await self.store.create_session(
                    agent_id,
                    task_summary,
                    intent,
                    work_scope,
                    details,
                    current_step,
                    now,
                    registered_at=resolution["session_started_at"],
                    source_instance_id=source,
                    global_expires_at=expires_at,
                )
            except IntegrityError:
                raced = await self.store.get_session(agent_id)
                if raced is None or raced["state"] != "active":
                    return None, self._foreign_unavailable(
                        agent_id,
                        "Local foreign-session attachment raced with another lifecycle change.",
                    )
        self._inc("terminal_mcp_agent_foreign_attachments_total")
        return await self.store.get_session(agent_id), None

    async def start(
        self,
        task_summary=None,
        intent=None,
        work_scope=None,
        details=None,
        agent_id=None,
    ):
        plan_supplied = any(
            value is not None for value in (task_summary, intent, details, work_scope)
        )

        if agent_id:
            current = await self.store.get_session(agent_id)
            if current is not None:
                current = await self._enforce_session(current)
                if current["state"] != "active":
                    if (
                        current.get("source_instance_id")
                        and current.get("source_instance_id") != self.local_instance_id
                        and current.get("end_reason") == "idle_timeout"
                    ):
                        attached, error = await self._attach_foreign(agent_id, current)
                        if error:
                            return error
                        current = attached
                    else:
                        return self.expired_result(agent_id, current)

                gate = await self.gate(agent_id, "agent_start", surface_messages=True)
                if gate.get("blocked"):
                    return gate["response"]
                if not plan_supplied:
                    return await self.overview(
                        agent_id=agent_id, touch=False, reveal_self_id=False
                    )

                resolved_details = current["details"] if details is None else details
                if not resolved_details:
                    return {
                        "ok": False,
                        "agent_name": public_agent_name(agent_id),
                        "error": "agent_start.details: at least one plan step is required",
                        **gate["context"],
                    }
                current_step = min(max(current["current_step"], 1), len(resolved_details))
                now = utc_text()
                await self.store.update_session(
                    agent_id,
                    task_summary if task_summary is not None else current["task_summary"],
                    intent if intent is not None else current["intent"],
                    current["work_scope"] if work_scope is None else work_scope,
                    resolved_details,
                    current_step,
                    now,
                )
                self._inc("terminal_mcp_agent_plan_updates_total")
                return await self.overview(
                    agent_id=agent_id, touch=False, reveal_self_id=False
                )

            if not is_agent_id(agent_id):
                return {
                    "ok": False,
                    "admission_required": True,
                    "agent_name": public_agent_name(agent_id),
                    "error": (
                        "agent_start.agent_id: expected a NATO name with 40-bit private suffix"
                    ),
                }

            proposal_cutoff = utc_text(
                utc_now() - timedelta(seconds=self.policy.public_name_reservation_seconds)
            )
            proposal = await self.store.proposal(agent_id, proposal_cutoff)
            if proposal is None:
                attached, error = await self._attach_foreign(agent_id)
                if error:
                    return error
                return await self.overview(
                    agent_id=agent_id, touch=False, reveal_self_id=True
                )

            missing = self._missing_registration_field(task_summary, intent, details)
            if missing:
                return {
                    "ok": False,
                    "admission_required": True,
                    "agent_name": public_agent_name(agent_id),
                    "error": missing,
                    "detail": (
                        "Provide task_summary, intent and details to confirm this locally "
                        "proposed full agent_id."
                    ),
                }

            async with self._start_lock:
                raced = await self.store.get_session(agent_id)
                if raced is not None:
                    raced = await self._enforce_session(raced)
                    if raced["state"] != "active":
                        return self.expired_result(agent_id, raced)
                    return await self.overview(
                        agent_id=agent_id, touch=False, reveal_self_id=False
                    )
                proposal = await self.store.proposal(agent_id, proposal_cutoff)
                if proposal is None:
                    return self._foreign_unavailable(
                        agent_id, "Local admission proposal expired before confirmation."
                    )
                now_dt = utc_now()
                now = utc_text(now_dt)
                expires_at = utc_text(
                    now_dt + timedelta(seconds=self.max_session_seconds)
                )
                try:
                    await self.store.create_session(
                        agent_id,
                        task_summary,
                        intent,
                        work_scope or [],
                        details,
                        1,
                        now,
                        source_instance_id=self.local_instance_id,
                        global_expires_at=expires_at,
                    )
                    await self.store.consume_proposal(agent_id)
                except IntegrityError:
                    existing = await self.store.get_session(agent_id)
                    if existing is None or existing["state"] != "active":
                        return self.expired_result(agent_id, existing)
                    return await self.overview(
                        agent_id=agent_id, touch=False, reveal_self_id=False
                    )

            self._inc("terminal_mcp_agent_sessions_total")
            return await self.overview(agent_id=agent_id, touch=False, reveal_self_id=True)

        missing = self._missing_registration_field(task_summary, intent, details)
        if missing:
            return {"ok": False, "admission_required": True, "error": missing}

        async with self._start_lock:
            now_dt = utc_now()
            reserved_names = await self._reserved_public_names(now_dt)
            for _ in range(64):
                proposed = generate_agent_id()
                if public_agent_name(proposed) in reserved_names:
                    continue
                if await self.store.get_session(proposed) is not None:
                    continue
                break
            else:
                raise RuntimeError("unable to allocate unique agent id")
            await self.store.create_proposal(proposed, utc_text(now_dt))

        return {
            "ok": False,
            "admission_required": True,
            "agent_name": public_agent_name(proposed),
            "proposed_agent_id": proposed,
            "detail": (
                "No work session was started. Reuse your existing full agent_id from another "
                "Terminal MCP if you have one; otherwise call agent_start again with "
                "agent_id=proposed_agent_id and the same plan to confirm this identity."
            ),
        }

    async def _enforce_session(self, session, now=None):
        if session is None or session["state"] != "active":
            return session
        current = now or utc_now()
        reason = session_expiry_reason(
            session,
            now=current,
            idle_ttl_seconds=self.ttl_seconds,
            max_session_seconds=self.max_session_seconds,
        )
        if reason:
            stamp = utc_text(current)
            await self.store.expire(session["agent_id"], reason=reason, now=stamp)
            if self.task_store is not None:
                claims = await self.task_store.claims_for_agent(
                    session["agent_id"], active_only=True
                )
                for claim in claims:
                    if not await self.task_store.release_claim(
                        claim["namespace"], claim["task_id"], session["agent_id"], now=stamp
                    ):
                        continue
                    await self.task_store.add_event(
                        claim["namespace"],
                        claim["task_id"],
                        "claim_released",
                        agent_id=session["agent_id"],
                        payload={"reason": reason},
                        now=stamp,
                    )
            self._inc("terminal_mcp_agent_sessions_expired_total")
            return await self.store.get_session(session["agent_id"])
        return session

    async def validate(self, agent_id, tool, *, touch=True):
        session = await self.store.get_session(agent_id)
        session = await self._enforce_session(session)
        if not session:
            if is_agent_id(agent_id):
                session, error = await self._attach_foreign(agent_id)
                if error:
                    return error
            else:
                return self.expired_result(agent_id, session)
        elif session["state"] != "active":
            if (
                session.get("source_instance_id")
                and session.get("source_instance_id") != self.local_instance_id
                and session.get("end_reason") == "idle_timeout"
            ):
                session, error = await self._attach_foreign(agent_id, session)
                if error:
                    return error
            else:
                return self.expired_result(agent_id, session)
        if touch:
            stamp = utc_text()
            await self.store.touch(agent_id, stamp)
            await self.store.activity(agent_id, tool, stamp)
        return None

    async def touch_if_active(self, agent_id, tool):
        if not agent_id:
            return None
        gate = await self.validate(agent_id, tool)
        return gate

    async def validate_task_lease(self, agent_id):
        latest_task_at = await self.store.latest_task_at(agent_id)
        now = utc_now()
        task_age = (
            (now - parse_utc(latest_task_at)).total_seconds() if latest_task_at else float("inf")
        )
        if task_age > self.task_context_ttl_seconds:
            self._inc("terminal_mcp_agent_task_lease_expired_total")
            return {
                "ok": False,
                "agent_name": public_agent_name(agent_id),
                "task_context_expired": True,
                "task_age_seconds": None if latest_task_at is None else max(0, int(task_age)),
                "max_task_age_seconds": self.task_context_ttl_seconds,
                "task_context_age_seconds": (
                    None if latest_task_at is None else max(0, int(task_age))
                ),
                "task_context_ttl_seconds": self.task_context_ttl_seconds,
            }
        return None

    async def session_context(self, agent_id):
        if not agent_id:
            return {}
        session = await self.store.get_session(agent_id)
        session = await self._enforce_session(session)
        if not session:
            return {"agent_name": public_agent_name(agent_id)}
        now = utc_now()
        registered = parse_utc(session["registered_at"])
        age = max(0, int((now - registered).total_seconds()))
        global_expires_at = session.get("global_expires_at")
        if global_expires_at:
            remaining = max(0, int((parse_utc(global_expires_at) - now).total_seconds()))
        else:
            remaining = max(0, self.max_session_seconds - age)
        latest_task = await self.store.latest_task_at(agent_id)
        task_age = (
            max(0, int((now - parse_utc(latest_task)).total_seconds())) if latest_task else None
        )
        warning = None
        if session["state"] == "active" and age >= self.session_warning_after_seconds:
            warning = (
                f"Session ends in {remaining}s. Reach a safe checkpoint and return to chat "
                "with an interim report. If a long build or test run is needed, start it now "
                "before returning so it can continue while the user reviews the report."
            )
        status = await self._session_status(session, task_age=task_age)
        return {
            "agent_name": public_agent_name(agent_id),
            "session_status": status,
            "session_started_at": session["registered_at"],
            "session_age_seconds": age,
            "session_remaining_seconds": remaining,
            "session_warning": warning,
            "session_end_reason": session["end_reason"],
            "task_age_seconds": task_age,
            "max_task_age_seconds": self.task_context_ttl_seconds,
            "task_context_age_seconds": task_age,
            "task_context_ttl_seconds": self.task_context_ttl_seconds,
            "preferred_queue_id": session["preferred_queue_id"],
        }

    async def _session_status(self, session, task_age=None):
        if session["state"] == "finished":
            return "finished"
        if session["state"] == "forced":
            return "forced"
        if task_age is None:
            latest_task = await self.store.latest_task_at(session["agent_id"])
            task_age = (
                max(0, int((utc_now() - parse_utc(latest_task)).total_seconds()))
                if latest_task
                else None
            )
        activity_count = await self.store.activity_count(session["agent_id"])
        if activity_count == 0:
            return "started"
        if task_age is None or task_age > self.task_context_ttl_seconds:
            return "idle"
        return "active"

    async def _ensure_session_alert(self, agent_id):
        if not agent_id or not self.policy.session_alert_enabled:
            return None
        now = utc_now()
        session = await self.store.get_session(agent_id)
        session = await self._enforce_session(session, now)
        if not session or session["state"] != "active":
            return None
        age = (now - parse_utc(session["registered_at"])).total_seconds()
        if age < self.policy.session_alert_after_seconds:
            return None
        repeat_cutoff = utc_text(now - timedelta(seconds=self.policy.session_alert_repeat_seconds))
        result = await self.store.ensure_system_alert(
            sender_agent_id=self.SESSION_ALERT_SENDER,
            recipient_agent_id=agent_id,
            target_name=public_agent_name(agent_id),
            text=self.policy.session_alert_message,
            now=utc_text(now),
            repeat_cutoff=repeat_cutoff,
        )
        if result.get("created"):
            self._inc("terminal_mcp_agent_session_alerts_total")
        return result

    async def message_state(self, agent_id, *, surface=False):
        if not agent_id:
            return {
                "pending_messages": [],
                "alert_messages": [],
                "reply_required_messages": [],
                "unread_message_pending": False,
                "reply_required_pending": False,
                "alert_pending": False,
            }
        await self._ensure_session_alert(agent_id)
        messages = await self.store.message_obligations(agent_id)
        now = utc_now()
        if surface and messages:
            await self.store.mark_messages_seen(
                agent_id, [item["message_hash"] for item in messages], utc_text(now)
            )
            messages = await self.store.message_obligations(agent_id)

        pending = []
        alerts = []
        replies = []
        unread = False
        reply_required = False
        alert_pending = False
        for item in messages:
            if item["read_at"] is None:
                unread = True
            if item["require_reply"] and item["replied_at"] is None:
                reply_required = True
            if item["alert"] and item["replied_at"] is None:
                alert_pending = True
            line = self._format_message(item, now)
            pending.append(line)
            if item["alert"] and item["replied_at"] is None:
                alerts.append(line)
            if item["require_reply"] and item["replied_at"] is None:
                replies.append(line)
        return {
            "pending_messages": pending,
            "alert_messages": alerts,
            "reply_required_messages": replies,
            "unread_message_pending": unread,
            "reply_required_pending": reply_required,
            "alert_pending": alert_pending,
        }

    @staticmethod
    def _message_obligation_record(item):
        if item["replied_at"]:
            state = "replied"
        elif item["read_at"]:
            state = "read"
        elif item["first_seen_at"]:
            state = "seen"
        else:
            state = "delivered"
        return {
            "message_hash": item["message_hash"],
            "state": state,
            "sender_name": public_agent_name(item["sender_agent_id"]),
            "text": item["text"],
            "require_reply": item["require_reply"],
            "alert": item["alert"],
            "namespace": item.get("task_namespace"),
            "task_id": item.get("task_id"),
            "created_at": item["created_at"],
            "first_seen_at": item["first_seen_at"],
            "read_at": item["read_at"],
            "replied_at": item["replied_at"],
            "seen_count": item["seen_count"],
        }

    def _format_message(self, item, now):
        if item["replied_at"]:
            state = "REPLIED"
        elif item["read_at"]:
            state = "READ · REPLY REQUIRED"
        elif item["first_seen_at"]:
            state = "SEEN · READ ACK REQUIRED"
        else:
            state = "DELIVERED · READ ACK REQUIRED"
        prefix = "ALERT · " if item["alert"] else ""
        sender = public_agent_name(item["sender_agent_id"])
        if item.get("task_namespace") and item.get("task_id"):
            target = f"task {item['task_namespace']}/{item['task_id']}"
        else:
            target = "all" if item["target_name"] is None else "you"
        full = True
        if item["first_seen_at"]:
            seen_age = (now - parse_utc(item["first_seen_at"])).total_seconds()
            full = (
                seen_age <= self.policy.message_reminder_seconds
                or item["seen_count"] <= self.policy.message_reminder_calls
            )
        body = item["text"] if full else "message reminder"
        actions = f"ack: message({item['message_hash']})"
        if item["require_reply"] and item["replied_at"] is None:
            actions += f" | reply: message({item['message_hash']}, text=...)"
        return (
            f"{prefix}{state} · {short_time(item['created_at'])} {item['message_hash']} "
            f"{sender} → {target}: {body} | {actions}"
        )

    async def gate(self, agent_id, tool, *, require_intent=False, surface_messages=True):
        lifecycle = await self.validate(agent_id, tool)
        if lifecycle:
            lifecycle.update(await self.session_context(agent_id))
            return {"blocked": True, "response": lifecycle}
        messages = await self.message_state(agent_id, surface=surface_messages)
        context = await self.session_context(agent_id)
        common = {**context, **messages}
        if messages["alert_pending"] and tool in self.ALERT_BLOCKED_TOOLS:
            return {
                "blocked": True,
                "response": {
                    "ok": False,
                    "error": (
                        "agent.alert: reply to the pending ALERT before continuing this operation"
                    ),
                    "coordination_message_pending": True,
                    **common,
                },
            }
        if tool == "run" and (
            messages["unread_message_pending"] or messages["reply_required_pending"]
        ):
            reason = (
                "reply to the required message"
                if messages["reply_required_pending"]
                else "acknowledge the unread message"
            )
            return {
                "blocked": True,
                "response": {
                    "ok": False,
                    "error": f"run.coordination: {reason} with message(...) before run",
                    "coordination_message_pending": True,
                    **common,
                },
            }
        if require_intent:
            task_gate = await self.validate_task_lease(agent_id)
            if task_gate:
                task_gate.setdefault(
                    "error", "run.task: task context expired; call coordinate with step and intent"
                )
                task_gate.update(common)
                return {"blocked": True, "response": task_gate}
        return {"blocked": False, "context": common}

    async def active_snapshot(self, exclude_agent_id=None):
        now = utc_now()
        active_cutoff = utc_text(now - timedelta(seconds=self.ttl_seconds))
        rows = await self.store.active_with_latest_command(
            active_cutoff,
            exclude_agent_id=exclude_agent_id,
            limit=self.policy.max_active_agents,
        )
        items = []
        for row in rows:
            session = await self.store.get_session(row["agent_id"])
            session = await self._enforce_session(session, now)
            if not session or session["state"] != "active":
                continue
            marker = row["last_command_hash"] or "started"
            queue = f" q{row['last_command_queue_id']}" if row["last_command_queue_id"] else ""
            status = f" {row['last_command_status']}" if row["last_command_status"] else ""
            items.append(
                (
                    session["last_activity_at"],
                    f"{relative_time(session['last_activity_at'], now)} "
                    f"{public_agent_name(row['agent_id'])} {marker} — {session['intent']}"
                    f"{queue}{status}",
                )
            )
        event_cutoff = utc_text(now - timedelta(seconds=self.policy.event_window_seconds))
        events = await self.store.recent_lifecycle(
            event_cutoff,
            exclude_agent_id=exclude_agent_id,
            limit=self.policy.max_active_agents * 2,
        )
        for event in events:
            if event["event"] not in {"finished", "forced"}:
                continue
            items.append(
                (
                    event["event_at"],
                    f"{relative_time(event['event_at'], now)} "
                    f"{public_agent_name(event['agent_id'])} {event['event']} — {event['intent']}",
                )
            )
        seen = set()
        compact = []
        for _, line in sorted(items, key=lambda item: item[0] or "", reverse=True):
            if line in seen:
                continue
            compact.append(line)
            seen.add(line)
        return compact[: self.policy.max_active_agents]

    async def pending_messages(self, agent_id, *, surface=False):
        return (await self.message_state(agent_id, surface=surface))["pending_messages"]

    async def record_command(self, agent_id, tool, command_hash):
        stamp = utc_text()
        await self.store.touch(agent_id, stamp)
        await self.store.activity(agent_id, tool, stamp, command_hash)
        self._inc("terminal_mcp_agent_commands_total")

    async def resolve_queue(self, agent_id, requested_queue_id, queue_count, least_loaded):
        if requested_queue_id is not None:
            if requested_queue_id < 1 or requested_queue_id > queue_count:
                raise ValueError(f"queue_id must be between 1 and {queue_count}")
            queue_id = requested_queue_id
        else:
            session = await self.store.get_session(agent_id) if agent_id else None
            queue_id = session["preferred_queue_id"] if session else None
            if queue_id is None or queue_id > queue_count:
                queue_id = await least_loaded()
        if agent_id:
            await self.store.set_preferred_queue(agent_id, queue_id)
        return queue_id

    async def coordinate(self, agent_id, step=None, intent=None, show_details=False):
        gate = await self.gate(agent_id, "coordinate", surface_messages=True)
        if gate["blocked"]:
            return gate["response"]
        current = await self.store.get_session(agent_id)
        if current is None:
            return self.expired_result(agent_id)
        details = current["details"]
        if intent is not None and step is None:
            return {
                "ok": False,
                "agent_name": public_agent_name(agent_id),
                "error": "coordinate.step: step is required when intent is provided",
                **gate["context"],
            }
        requested_step = current["current_step"] if step is None else step
        if requested_step < 1 or requested_step > len(details):
            return {
                "ok": False,
                "agent_name": public_agent_name(agent_id),
                "error": f"coordinate.step: must be between 1 and {len(details)}",
                **gate["context"],
            }
        if intent is not None:
            now = utc_text()
            await self.store.update_coordinate(agent_id, intent, requested_step, now)
            self._inc("terminal_mcp_agent_task_updates_total")
            current = await self.store.get_session(agent_id)

        other_details = []
        if show_details:
            cutoff = utc_text(utc_now() - timedelta(seconds=self.ttl_seconds))
            sessions = await self.store.active(cutoff, self.policy.max_active_agents + 1)
            for session in sessions:
                if session["agent_id"] == agent_id:
                    continue
                plan = " | ".join(
                    f"{index}. {value}" for index, value in enumerate(session["details"], start=1)
                )
                other_details.append(
                    f"{public_agent_name(session['agent_id'])} "
                    f"step {session['current_step']}/{len(session['details'])} — "
                    f"{session['intent']} | {plan}"
                )
        context = {**await self.session_context(agent_id), **await self.message_state(agent_id)}
        return {
            "ok": True,
            "agent_name": public_agent_name(agent_id),
            "step": requested_step,
            "intent": current["intent"],
            "detail": details[requested_step - 1],
            "other_details": other_details,
            "active_agents": await self.active_snapshot(exclude_agent_id=agent_id),
            **context,
        }

    async def _message_grace_remaining(self, agent_id, message_hash):
        session = await self.store.get_session(agent_id)
        if not session or session["state"] != "finished" or not session.get("ended_at"):
            return None
        elapsed = (utc_now() - parse_utc(session["ended_at"])).total_seconds()
        if elapsed < 0 or elapsed > self.policy.post_finish_message_grace_seconds:
            return None
        if await self.store.recipient_record(message_hash, agent_id) is None:
            return None
        return max(0, int(self.policy.post_finish_message_grace_seconds - elapsed))

    async def message(
        self,
        agent_id,
        text=None,
        target=None,
        message_hash=None,
        require_reply=False,
        alert=False,
        namespace=None,
        task_id=None,
    ):
        lifecycle = await self.validate(agent_id, "message")
        require_reply = bool(require_reply or alert)
        grace_remaining = None
        if lifecycle:
            if message_hash is None:
                return lifecycle
            grace_remaining = await self._message_grace_remaining(agent_id, message_hash)
            if grace_remaining is None:
                return lifecycle
        if message_hash is not None:
            original = await self.store.message_record(message_hash)
            if original is None:
                return {
                    "ok": False,
                    "agent_name": public_agent_name(agent_id),
                    "message_hash": message_hash,
                    "error": "message.lookup: message not found",
                }
            recipient = await self.store.recipient_record(message_hash, agent_id)
            if text is None:
                if (
                    target is not None
                    or require_reply
                    or alert
                    or namespace is not None
                    or task_id is not None
                ):
                    return {
                        "ok": False,
                        "agent_name": public_agent_name(agent_id),
                        "message_hash": message_hash,
                        "error": "message.mode: acknowledgement accepts message_hash only",
                    }
                if recipient is not None:
                    await self.store.acknowledge_message(message_hash, agent_id, utc_text())
                elif original["sender_agent_id"] != agent_id:
                    return {
                        "ok": False,
                        "agent_name": public_agent_name(agent_id),
                        "message_hash": message_hash,
                        "error": "message.ack: caller is neither sender nor recipient",
                    }
                receipts = await self.store.message_receipts(message_hash)
                response = await self._message_response(agent_id, message_hash, receipts)
                response["namespace"] = original.get("task_namespace")
                response["task_id"] = original.get("task_id")
                if grace_remaining is not None:
                    response["message_grace_remaining_seconds"] = grace_remaining
                return response

            if (
                target is not None
                or require_reply
                or alert
                or namespace is not None
                or task_id is not None
            ):
                return {
                    "ok": False,
                    "agent_name": public_agent_name(agent_id),
                    "message_hash": message_hash,
                    "error": (
                        "message.reply: target/require_reply/alert are inherited "
                        "from the original message"
                    ),
                }
            if recipient is None:
                return {
                    "ok": False,
                    "agent_name": public_agent_name(agent_id),
                    "message_hash": message_hash,
                    "error": "message.reply: caller is not a recipient of the original message",
                }
            reply_hash = await self._create_message(
                agent_id,
                text,
                public_agent_name(original["sender_agent_id"]),
                [original["sender_agent_id"]],
                False,
                False,
                task_namespace=original.get("task_namespace"),
                task_id=original.get("task_id"),
            )
            replied_at = utc_text()
            await self.store.mark_replied(message_hash, agent_id, reply_hash, replied_at)
            if (
                original.get("task_namespace")
                and original.get("task_id")
                and self.task_store is not None
            ):
                await self.task_store.add_event(
                    original["task_namespace"],
                    original["task_id"],
                    "message",
                    agent_id=agent_id,
                    payload={
                        "message_hash": reply_hash,
                        "reply_to": message_hash,
                        "text": text,
                        "delivered_to": [public_agent_name(original["sender_agent_id"])],
                    },
                    now=replied_at,
                )
            receipts = await self.store.message_receipts(message_hash)
            response = await self._message_response(agent_id, message_hash, receipts)
            response["reply_message_hash"] = reply_hash
            response["delivered_to"] = [public_agent_name(original["sender_agent_id"])]
            response["namespace"] = original.get("task_namespace")
            response["task_id"] = original.get("task_id")
            if grace_remaining is not None:
                response["message_grace_remaining_seconds"] = grace_remaining
            return response

        routing_error = validate_message_routing(
            target=target, namespace=namespace, task_id=task_id, alert=alert
        )
        if routing_error:
            return {
                "ok": False,
                "agent_name": public_agent_name(agent_id),
                "error": routing_error,
            }
        if target and target.casefold() == "broadcast":
            target = None

        if not text:
            return {
                "ok": False,
                "agent_name": public_agent_name(agent_id),
                "error": "message.text: text is required when sending",
            }
        if target and public_agent_name(target) != target:
            return {
                "ok": False,
                "agent_name": public_agent_name(agent_id),
                "error": "message.target: use the public agent name without internal suffix",
            }
        cutoff = utc_text(utc_now() - timedelta(seconds=self.ttl_seconds))
        sessions = await self.store.active(cutoff, self.policy.max_active_agents + 1)
        active = []
        for item in sessions:
            current = await self._enforce_session(item)
            if current and current["state"] == "active" and current["agent_id"] != agent_id:
                active.append(current)
        recipients = active
        if namespace is not None:
            if self.task_store is None:
                return {
                    "ok": False,
                    "agent_name": public_agent_name(agent_id),
                    "error": "message.task: task coordination unavailable",
                }
            task = await self.task_store.get_task(namespace, task_id)
            if task is None:
                return {
                    "ok": False,
                    "agent_name": public_agent_name(agent_id),
                    "error": f"message.task: unknown task {namespace}/{task_id}",
                }
            claimed = {
                item["agent_id"] for item in await self.task_store.active_claims(namespace, task_id)
            }
            recipients = [item for item in active if item["agent_id"] in claimed]
        elif target:
            recipients = [
                item for item in recipients if public_agent_name(item["agent_id"]) == target
            ]
            if not recipients:
                return {
                    "ok": False,
                    "agent_name": public_agent_name(agent_id),
                    "error": f"message.target: active agent {target!r} not found",
                }
        if not recipients and namespace is None:
            return {
                "ok": False,
                "agent_name": public_agent_name(agent_id),
                "error": "message.recipients: no active recipients",
            }
        allocated_hash = await self._create_message(
            agent_id,
            text,
            target,
            [item["agent_id"] for item in recipients],
            require_reply,
            alert,
            task_namespace=namespace,
            task_id=task_id,
        )
        self._inc("terminal_mcp_coordination_messages_total")
        if namespace is not None and self.task_store is not None:
            await self.task_store.add_event(
                namespace,
                task_id,
                "message",
                agent_id=agent_id,
                payload={
                    "message_hash": allocated_hash,
                    "text": text,
                    "require_reply": require_reply,
                    "alert": bool(alert),
                    "delivered_to": [public_agent_name(item["agent_id"]) for item in recipients],
                },
            )
        return {
            "ok": True,
            "agent_name": public_agent_name(agent_id),
            "message_hash": allocated_hash,
            "namespace": namespace,
            "task_id": task_id,
            "delivered_to": [public_agent_name(item["agent_id"]) for item in recipients],
            "seen_by": [],
            "read_by": [],
            "replied_by": [],
            "inactive_recipients": [],
            **await self.session_context(agent_id),
            **await self.message_state(agent_id),
        }

    async def _create_message(
        self,
        sender_agent_id,
        text,
        target_name,
        recipient_ids,
        require_reply,
        alert,
        *,
        task_namespace=None,
        task_id=None,
    ):
        created_at = utc_text()
        for _ in range(32):
            allocated_hash = secrets.token_hex(4)
            try:
                await self.store.create_message(
                    allocated_hash,
                    sender_agent_id,
                    target_name,
                    text,
                    created_at,
                    recipient_ids,
                    require_reply=require_reply,
                    alert=alert,
                    task_namespace=task_namespace,
                    task_id=task_id,
                )
                return allocated_hash
            except IntegrityError:
                continue
        raise RuntimeError("unable to allocate unique message hash")

    async def _message_response(self, agent_id, message_hash, receipts):
        now = utc_now()
        inactive_recipients = []
        for item in receipts:
            session = await self._enforce_session(
                await self.store.get_session(item["agent_id"]), now
            )
            if session is None or session["state"] != "active":
                inactive_recipients.append(public_agent_name(item["agent_id"]))
        return {
            "ok": True,
            "agent_name": public_agent_name(agent_id),
            "message_hash": message_hash,
            "delivered_to": [public_agent_name(item["agent_id"]) for item in receipts],
            "seen_by": [public_agent_name(item["agent_id"]) for item in receipts if item["seen"]],
            "read_by": [public_agent_name(item["agent_id"]) for item in receipts if item["read"]],
            "replied_by": [
                public_agent_name(item["agent_id"]) for item in receipts if item["replied"]
            ],
            "inactive_recipients": inactive_recipients,
        }

    async def message_obligation_summary(self, agent_id):
        obligations = await self.store.message_obligations(agent_id)
        return {
            "unacknowledged": [
                item["message_hash"] for item in obligations if item["read_at"] is None
            ],
            "reply_required": [
                item["message_hash"]
                for item in obligations
                if item["require_reply"] and item["replied_at"] is None
            ],
            "alerts": [
                item["message_hash"]
                for item in obligations
                if item["alert"] and item["replied_at"] is None
            ],
        }

    async def finish(self, agent_id):
        lifecycle = await self.validate(agent_id, "agent_finish")
        if lifecycle:
            return lifecycle
        await self.store.finish(agent_id, utc_text())
        return {
            "ok": True,
            "agent_name": public_agent_name(agent_id),
            "finished": True,
            **await self.session_context(agent_id),
        }

    async def _managed_task_refs(self, agent_id):
        if self.task_store is None:
            return []
        priorities = {3: "P0", 2: "P1", 1: "P2", 0: "P3"}
        rows = await self.task_store.claims_for_agent(agent_id, active_only=True)
        result = []
        for item in rows:
            claims = await live_task_claims(
                self.task_store,
                self.store,
                item["namespace"],
                item["task_id"],
                idle_ttl_seconds=self.ttl_seconds,
                max_session_seconds=self.max_session_seconds,
            )
            own = next((claim for claim in claims if claim["agent_id"] == agent_id), None)
            if own is None:
                continue
            role = "owner" if claims[0]["agent_id"] == agent_id else "participant"
            claim_age_seconds = max(
                0, int((utc_now() - parse_utc(item["claimed_at"])).total_seconds())
            )
            result.append(
                {
                    "namespace": item["namespace"],
                    "task_id": item["task_id"],
                    "lane": item["lane"],
                    "priority": priorities.get(int(item["priority"]), "P3"),
                    "state": item["state"],
                    "operational_status": item["state"]
                    if item["state"] != "ready"
                    else "in_progress",
                    "isolation_hint": item["isolation_hint"],
                    "claimed_at": item["claimed_at"],
                    "claim_age_seconds": claim_age_seconds,
                    "claim_intent": item.get("claim_intent") or "",
                    "role": role,
                }
            )
        return result

    async def overview(
        self,
        agent_id=None,
        *,
        target=None,
        target_session_ref=None,
        show_details=False,
        show_intents=False,
        show_commands=False,
        command_hash=None,
        since_minutes=None,
        touch=True,
        reveal_self_id=False,
    ):
        caller_context = {}
        if agent_id and touch:
            gate = await self.gate(agent_id, "agents", surface_messages=True)
            if gate["blocked"]:
                return gate["response"]
            caller_context = gate["context"]
        elif agent_id:
            caller_context = {
                **await self.session_context(agent_id),
                **await self.message_state(agent_id),
            }

        if command_hash:
            detail = await self.store.command_detail(command_hash)
            if detail and detail.get("agent_id"):
                detail["agent_name"] = public_agent_name(detail["agent_id"])
                detail.pop("agent_id", None)
            return {"ok": True, "command": detail, **caller_context}

        minutes = since_minutes or self.policy.history_default_minutes
        now = utc_now()
        cutoff = utc_text(now - timedelta(minutes=max(1, minutes)))
        sessions = await self.store.history_sessions(cutoff, limit=100)
        normalized = []
        for session in sessions:
            current = await self._enforce_session(session, now)
            if current:
                normalized.append(current)
        sessions = normalized
        if target:
            sessions = [s for s in sessions if public_agent_name(s["agent_id"]) == target]
        if target_session_ref:
            sessions = [
                s
                for s in sessions
                if public_session_ref(
                    s["agent_id"],
                    s.get("source_instance_id"),
                    s.get("registered_at"),
                )
                == target_session_ref
            ]
        if target or target_session_ref:
            sessions = sessions[:1]

        records = []
        for session in sessions:
            latest_task = await self.store.latest_task_at(session["agent_id"])
            task_age = (
                max(0, int((now - parse_utc(latest_task)).total_seconds())) if latest_task else None
            )
            recent = await self.store.recent_commands(session["agent_id"], 1)
            last_command = recent[0] if recent else None
            idle_seconds = max(
                0, int((now - parse_utc(session["last_activity_at"])).total_seconds())
            )
            session_age_seconds = max(
                0, int((now - parse_utc(session["registered_at"])).total_seconds())
            )
            source_instance_id = session.get("source_instance_id") or self.local_instance_id
            global_expires_at = session.get("global_expires_at")
            remaining = (
                max(0, int((parse_utc(global_expires_at) - now).total_seconds()))
                if global_expires_at
                else max(0, self.max_session_seconds - session_age_seconds)
            )
            record = {
                "name": public_agent_name(session["agent_id"]),
                "session_ref": public_session_ref(
                    session["agent_id"], source_instance_id, session["registered_at"]
                ),
                "origin_instance_id": source_instance_id,
                "session_started_at": session["registered_at"],
                "session_remaining_seconds": remaining,
                "attachment": bool(
                    source_instance_id
                    and self.local_instance_id
                    and source_instance_id != self.local_instance_id
                ),
                "status": await self._session_status(session, task_age=task_age),
                "last_activity": relative_time(session["last_activity_at"], now),
                "last_activity_at": session["last_activity_at"],
                "idle_seconds": idle_seconds,
                "session_age_seconds": session_age_seconds,
                "intent": session["intent"],
                "current_step": session["current_step"],
                "end_reason": session["end_reason"],
            }
            obligations = await self.store.message_obligations(session["agent_id"])
            activity = await self.store.latest_activity(session["agent_id"])
            record["last_activity_tool"] = activity["tool"] if activity else None
            record["last_activity_command_hash"] = activity["command_hash"] if activity else None
            unread = sum(1 for m in obligations if m["read_at"] is None)
            replies = sum(1 for m in obligations if m["require_reply"] and m["replied_at"] is None)
            alerts = sum(1 for m in obligations if m["alert"] and m["replied_at"] is None)
            if unread:
                record["messages_awaiting_read"] = unread
            if replies:
                record["messages_awaiting_reply"] = replies
            if alerts:
                record["alerts_pending"] = alerts
            if target:
                message_journal = await self.store.message_journal(session["agent_id"], cutoff)
                record["message_journal"] = [
                    self._message_obligation_record(item) for item in message_journal
                ]
            if show_commands or target:
                record["last_command"] = last_command
                record["preferred_queue_id"] = session["preferred_queue_id"]
            if show_details:
                record.update(
                    {
                        "task_summary": session["task_summary"],
                        "details": session["details"],
                        "work_scope": session["work_scope"],
                        "registered_at": session["registered_at"],
                        "ended_at": session["ended_at"],
                    }
                )
            managed_tasks = await self._managed_task_refs(session["agent_id"])
            if managed_tasks:
                record["managed_tasks"] = managed_tasks
            records.append(record)

        selected = sessions[0] if sessions else None
        intent_journal = (
            await self.store.intent_journal(selected["agent_id"], cutoff)
            if selected and show_intents
            else []
        )
        command_journal = (
            await self.store.command_journal(selected["agent_id"], cutoff)
            if selected and show_commands
            else []
        )

        active_records = []
        for record in records:
            if record["status"] in {"started", "active", "idle"}:
                session = next(
                    s for s in sessions if public_agent_name(s["agent_id"]) == record["name"]
                )
                idle = max(0, int((now - parse_utc(session["last_activity_at"])).total_seconds()))
                if agent_id and session["agent_id"] == agent_id:
                    continue
                active_record = {
                    "name": record["name"],
                    "status": record["status"],
                    "session_age_seconds": record["session_age_seconds"],
                    "idle_seconds": idle,
                    "intent": session["intent"],
                    "current_step": session["current_step"],
                }
                if show_details:
                    active_record["task_summary"] = session["task_summary"]
                    active_record["work_scope"] = session["work_scope"]
                if show_commands:
                    active_record["recent_commands"] = await self.store.recent_commands(
                        session["agent_id"], 3
                    )
                managed_tasks = await self._managed_task_refs(session["agent_id"])
                if managed_tasks:
                    active_record["managed_tasks"] = managed_tasks
                active_records.append(active_record)

        own = await self.store.get_session(agent_id) if agent_id else None
        self_payload = None
        if own:
            self_payload = {
                "name": public_agent_name(agent_id),
                "ttl_seconds": self.ttl_seconds,
                "task_context_ttl_seconds": self.task_context_ttl_seconds,
                "task_lease_seconds": self.task_context_ttl_seconds,
                "task_summary": own["task_summary"],
                "intent": own["intent"],
                "work_scope": own["work_scope"],
                "details": own["details"],
                "current_step": own["current_step"],
                "preferred_queue_id": own["preferred_queue_id"],
            }
            managed_tasks = await self._managed_task_refs(agent_id)
            if managed_tasks:
                self_payload["managed_tasks"] = managed_tasks
            if reveal_self_id:
                self_payload["agent_id"] = agent_id
        overlaps = find_scope_overlaps(own["work_scope"] if own else [], active_records)
        return {
            "ok": True,
            "self": self_payload,
            "active": active_records[: self.policy.max_active_agents],
            "sessions": records,
            "intent_journal": intent_journal or None,
            "command_journal": command_journal or None,
            "overlaps": overlaps or None,
            "additional_active_agents": max(0, len(active_records) - self.policy.max_active_agents),
            **caller_context,
        }

    @staticmethod
    def expired_result(agent_id, session=None):
        result = {
            "ok": False,
            "agent_name": public_agent_name(agent_id),
            "session_expired": True,
            "registration_required": True,
        }
        if session:
            result["session_status"] = "finished" if session["state"] == "finished" else "forced"
            result["session_started_at"] = session["registered_at"]
            result["session_end_reason"] = session.get("end_reason")
            result["return_to_chat"] = True
        return result

    def _inc(self, name, amount=1):
        if self.metrics:
            self.metrics.inc(name, value=amount)
