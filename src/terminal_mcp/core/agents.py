from __future__ import annotations

import secrets
from datetime import timedelta
from sqlite3 import IntegrityError

from terminal_mcp.core.agent_policy import AgentPolicy
from terminal_mcp.core.orchestration import (
    find_scope_overlaps,
    generate_agent_id,
    parse_utc,
    public_agent_name,
    relative_time,
    short_time,
    utc_now,
    utc_text,
)


class AgentCoordinator:
    ALERT_BLOCKED_TOOLS = {"run", "read", "coordinate", "agents", "agent_start"}

    def __init__(self, store, metrics=None, policy: AgentPolicy | None = None):
        self.store = store
        self.metrics = metrics
        self.policy = policy or AgentPolicy()
        # Mutable aliases preserve the existing testing/diagnostic surface.
        self.ttl_seconds = self.policy.idle_ttl_seconds
        self.task_lease_seconds = self.policy.intent_ttl_seconds
        self.max_session_seconds = self.policy.max_session_seconds
        self.session_warning_seconds = self.policy.session_warning_seconds

    async def start(
        self,
        task_summary=None,
        intent=None,
        work_scope=None,
        details=None,
        agent_id=None,
    ):
        if agent_id:
            gate = await self.gate(agent_id, "agent_start", surface_messages=True)
            if gate.get("blocked"):
                return gate["response"]
            if all(value is None for value in (task_summary, intent, details, work_scope)):
                return {
                    "ok": False,
                    "agent_name": public_agent_name(agent_id),
                    "error": "agent_start.update: provide at least one field to change",
                    **gate["context"],
                }
            current = await self.store.get_session(agent_id)
            if current is None:
                return self.expired_result(agent_id)
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
            return await self.overview(agent_id=agent_id, touch=False, reveal_self_id=False)

        if not task_summary:
            return {"ok": False, "error": "agent_start.task_summary: required for registration"}
        if not intent:
            return {"ok": False, "error": "agent_start.intent: required for registration"}
        if not details:
            return {"ok": False, "error": "agent_start.details: required for registration"}

        now_dt = utc_now()
        now = utc_text(now_dt)
        reserve_window = max(self.ttl_seconds, self.max_session_seconds)
        cutoff = utc_text(now_dt - timedelta(seconds=reserve_window))
        recent = await self.store.recent_sessions(cutoff)
        reserved_names = {public_agent_name(item["agent_id"]) for item in recent}
        for _ in range(64):
            allocated = generate_agent_id()
            if public_agent_name(allocated) in reserved_names:
                continue
            try:
                await self.store.create_session(
                    allocated,
                    task_summary,
                    intent,
                    work_scope or [],
                    details,
                    1,
                    now,
                )
                agent_id = allocated
                break
            except IntegrityError:
                continue
        else:
            raise RuntimeError("unable to allocate unique agent id")
        self._inc("terminal_mcp_agent_sessions_total")
        return await self.overview(agent_id=agent_id, touch=False, reveal_self_id=True)

    async def _enforce_session(self, session, now=None):
        if session is None or session["state"] != "active":
            return session
        current = now or utc_now()
        age = (current - parse_utc(session["registered_at"])).total_seconds()
        idle = (current - parse_utc(session["last_activity_at"])).total_seconds()
        reason = None
        if age >= self.max_session_seconds:
            reason = "max_session_duration"
        elif idle >= self.ttl_seconds:
            reason = "idle_timeout"
        if reason:
            await self.store.expire(session["agent_id"], reason=reason, now=utc_text(current))
            self._inc("terminal_mcp_agent_sessions_expired_total")
            return await self.store.get_session(session["agent_id"])
        return session

    async def validate(self, agent_id, tool, *, touch=True):
        session = await self.store.get_session(agent_id)
        session = await self._enforce_session(session)
        if not session or session["state"] != "active":
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
            (now - parse_utc(latest_task_at)).total_seconds()
            if latest_task_at
            else float("inf")
        )
        if task_age > self.task_lease_seconds:
            self._inc("terminal_mcp_agent_task_lease_expired_total")
            return {
                "ok": False,
                "agent_name": public_agent_name(agent_id),
                "task_context_expired": True,
                "task_age_seconds": None if latest_task_at is None else max(0, int(task_age)),
                "max_task_age_seconds": self.task_lease_seconds,
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
        remaining = max(0, self.max_session_seconds - age)
        latest_task = await self.store.latest_task_at(agent_id)
        task_age = (
            max(0, int((now - parse_utc(latest_task)).total_seconds()))
            if latest_task
            else None
        )
        warning = None
        if session["state"] == "active" and remaining <= self.session_warning_seconds:
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
            "max_task_age_seconds": self.task_lease_seconds,
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
        if task_age is None or task_age > self.task_lease_seconds:
            return "idle"
        return "active"

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

    async def message(
        self,
        agent_id,
        text=None,
        target=None,
        message_hash=None,
        require_reply=False,
        alert=False,
    ):
        lifecycle = await self.validate(agent_id, "message")
        if lifecycle:
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
                if target is not None or require_reply or alert:
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
                return self._message_response(agent_id, message_hash, receipts)

            if target is not None or require_reply or alert:
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
            )
            await self.store.mark_replied(message_hash, agent_id, reply_hash, utc_text())
            receipts = await self.store.message_receipts(message_hash)
            response = self._message_response(agent_id, message_hash, receipts)
            response["reply_message_hash"] = reply_hash
            response["delivered_to"] = [public_agent_name(original["sender_agent_id"])]
            return response

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
        if target:
            recipients = [
                item for item in recipients if public_agent_name(item["agent_id"]) == target
            ]
            if not recipients:
                return {
                    "ok": False,
                    "agent_name": public_agent_name(agent_id),
                    "error": f"message.target: active agent {target!r} not found",
                }
        if not recipients:
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
            require_reply or alert,
            alert,
        )
        self._inc("terminal_mcp_coordination_messages_total")
        return {
            "ok": True,
            "agent_name": public_agent_name(agent_id),
            "message_hash": allocated_hash,
            "delivered_to": [public_agent_name(item["agent_id"]) for item in recipients],
            "seen_by": [],
            "read_by": [],
            "replied_by": [],
            **await self.session_context(agent_id),
            **await self.message_state(agent_id),
        }

    async def _create_message(
        self, sender_agent_id, text, target_name, recipient_ids, require_reply, alert
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
                )
                return allocated_hash
            except IntegrityError:
                continue
        raise RuntimeError("unable to allocate unique message hash")

    def _message_response(self, agent_id, message_hash, receipts):
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

    async def overview(
        self,
        agent_id=None,
        *,
        target=None,
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
            sessions = sessions[:1]

        records = []
        for session in sessions:
            latest_task = await self.store.latest_task_at(session["agent_id"])
            task_age = (
                max(0, int((now - parse_utc(latest_task)).total_seconds()))
                if latest_task
                else None
            )
            recent = await self.store.recent_commands(session["agent_id"], 1)
            last_command = recent[0] if recent else None
            record = {
                "name": public_agent_name(session["agent_id"]),
                "status": await self._session_status(session, task_age=task_age),
                "last_activity": relative_time(session["last_activity_at"], now),
                "last_activity_at": session["last_activity_at"],
                "intent": session["intent"],
                "current_step": session["current_step"],
                "preferred_queue_id": session["preferred_queue_id"],
                "last_command": last_command,
                "end_reason": session["end_reason"],
            }
            obligations = await self.store.message_obligations(session["agent_id"])
            activity = await self.store.latest_activity(session["agent_id"])
            record["last_activity_tool"] = activity["tool"] if activity else None
            record["last_activity_command_hash"] = activity["command_hash"] if activity else None
            record["messages_awaiting_read"] = sum(1 for m in obligations if m["read_at"] is None)
            record["messages_awaiting_reply"] = sum(
                1 for m in obligations if m["require_reply"] and m["replied_at"] is None
            )
            record["alerts_pending"] = sum(
                1 for m in obligations if m["alert"] and m["replied_at"] is None
            )
            if target:
                message_journal = await self.store.message_journal(session["agent_id"], cutoff)
                record["message_journal"] = [
                    self._message_obligation_record(item) for item in message_journal
                ]
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
                active_records.append(
                    {
                        "name": record["name"],
                        "idle_seconds": idle,
                        "task_summary": session["task_summary"],
                        "intent": session["intent"],
                        "work_scope": session["work_scope"],
                        "current_step": session["current_step"],
                        "recent_commands": await self.store.recent_commands(session["agent_id"], 3),
                    }
                )

        own = await self.store.get_session(agent_id) if agent_id else None
        self_payload = None
        if own:
            self_payload = {
                "name": public_agent_name(agent_id),
                "ttl_seconds": self.ttl_seconds,
                "task_lease_seconds": self.task_lease_seconds,
                "task_summary": own["task_summary"],
                "intent": own["intent"],
                "work_scope": own["work_scope"],
                "details": own["details"],
                "current_step": own["current_step"],
                "preferred_queue_id": own["preferred_queue_id"],
            }
            if reveal_self_id:
                self_payload["agent_id"] = agent_id
        overlaps = find_scope_overlaps(own["work_scope"] if own else [], active_records)
        return {
            "ok": True,
            "self": self_payload,
            "active": active_records[: self.policy.max_active_agents],
            "sessions": records,
            "intent_journal": intent_journal,
            "command_journal": command_journal,
            "overlaps": overlaps,
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
        return result

    def _inc(self, name, amount=1):
        if self.metrics:
            self.metrics.inc(name, value=amount)
