from __future__ import annotations

import secrets
from datetime import timedelta
from sqlite3 import IntegrityError

from terminal_mcp.core.orchestration import (
    AGENT_EVENT_WINDOW_SECONDS,
    AGENT_TTL_SECONDS,
    MAX_ACTIVE_AGENTS,
    MAX_RECENT_COMMANDS,
    TASK_LEASE_SECONDS,
    find_scope_overlaps,
    generate_agent_id,
    parse_utc,
    public_agent_name,
    short_time,
    utc_now,
    utc_text,
)


class AgentCoordinator:
    def __init__(
        self,
        store,
        metrics=None,
        ttl_seconds=AGENT_TTL_SECONDS,
        task_lease_seconds=TASK_LEASE_SECONDS,
    ):
        self.store = store
        self.metrics = metrics
        self.ttl_seconds = ttl_seconds
        self.task_lease_seconds = task_lease_seconds

    async def start(
        self,
        task_summary=None,
        intent=None,
        work_scope=None,
        details=None,
        agent_id=None,
    ):
        if agent_id:
            if all(
                value is None
                for value in (task_summary, intent, details, work_scope)
            ):
                return {
                    "ok": False,
                    "agent_name": public_agent_name(agent_id),
                    "error": "agent_start.update: provide at least one field to change",
                }
            gate = await self.validate(agent_id, "agent_start")
            if gate:
                return gate
            current = await self.store.get_session(agent_id)
            if current is None:
                return self.expired_result(agent_id)
            resolved_details = current["details"] if details is None else details
            if not resolved_details:
                return {
                    "ok": False,
                    "agent_name": public_agent_name(agent_id),
                    "error": "agent_start.details: at least one plan step is required",
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
            return await self.overview(agent_id, touch=False, reveal_self_id=False)

        if not task_summary:
            return {"ok": False, "error": "agent_start.task_summary: required for registration"}
        if not intent:
            return {"ok": False, "error": "agent_start.intent: required for registration"}
        if not details:
            return {"ok": False, "error": "agent_start.details: required for registration"}

        now_dt = utc_now()
        now = utc_text(now_dt)
        cutoff = utc_text(now_dt - timedelta(seconds=self.ttl_seconds))
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
        return await self.overview(agent_id, touch=False, reveal_self_id=True)

    async def validate(self, agent_id, tool):
        session = await self.store.get_session(agent_id)
        if not session:
            return self.expired_result(agent_id)
        now = utc_now()
        idle = (now - parse_utc(session["last_activity_at"])).total_seconds()
        if session["state"] != "active" or idle > self.ttl_seconds:
            if session["state"] == "active":
                await self.store.expire(agent_id)
                self._inc("terminal_mcp_agent_sessions_expired_total")
            return self.expired_result(agent_id)
        stamp = utc_text(now)
        await self.store.touch(agent_id, stamp)
        await self.store.activity(agent_id, tool, stamp)
        return None

    async def touch_if_active(self, agent_id, tool):
        if not agent_id:
            return
        session = await self.store.get_session(agent_id)
        if not session or session["state"] != "active":
            return
        now = utc_now()
        idle = (now - parse_utc(session["last_activity_at"])).total_seconds()
        if idle > self.ttl_seconds:
            await self.store.expire(agent_id)
            self._inc("terminal_mcp_agent_sessions_expired_total")
            return
        stamp = utc_text(now)
        await self.store.touch(agent_id, stamp)
        await self.store.activity(agent_id, tool, stamp)

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

    async def validate_run(self, agent_id):
        gate = await self.validate(agent_id, "run")
        if gate:
            return gate
        return await self.validate_task_lease(agent_id)

    async def active_snapshot(self, exclude_agent_id=None):
        now = utc_now()
        active_cutoff = utc_text(now - timedelta(seconds=self.ttl_seconds))
        event_cutoff = utc_text(now - timedelta(seconds=AGENT_EVENT_WINDOW_SECONDS))
        rows = await self.store.active_with_latest_command(
            active_cutoff, exclude_agent_id=exclude_agent_id, limit=MAX_ACTIVE_AGENTS
        )
        events = await self.store.recent_lifecycle(
            event_cutoff, exclude_agent_id=exclude_agent_id, limit=MAX_ACTIVE_AGENTS * 2
        )

        items = []
        for row in rows:
            marker = row["last_command_hash"] or "started"
            event_at = (
                row["last_command_finished_at"]
                or row["last_command_started_at"]
                or row["registered_at"]
            )
            items.append(
                (
                    event_at,
                    f"{short_time(event_at)} {public_agent_name(row['agent_id'])} "
                    f"{marker} — {row['intent']}",
                )
            )
        for event in events:
            if event["event"] != "finished":
                continue
            items.append(
                (
                    event["event_at"],
                    f"{short_time(event['event_at'])} {public_agent_name(event['agent_id'])} "
                    f"finished — {event['intent']}",
                )
            )

        seen = set()
        compact = []
        for _, line in sorted(items, key=lambda item: item[0] or "", reverse=True):
            if line not in seen:
                compact.append(line)
                seen.add(line)
        return compact

    async def pending_messages(self, agent_id):
        if not agent_id:
            return []
        messages = await self.store.pending_messages(agent_id)
        return [
            (
                f"{short_time(item['created_at'])} {item['message_hash']} "
                f"{public_agent_name(item['sender_agent_id'])} → "
                f"{'all' if item['target_name'] is None else 'you'}: {item['text']} "
                f"| ack: message({item['message_hash']})"
            )
            for item in messages
        ]

    async def record_command(self, agent_id, tool, command_hash):
        stamp = utc_text()
        await self.store.touch(agent_id, stamp)
        await self.store.activity(agent_id, tool, stamp, command_hash)
        self._inc("terminal_mcp_agent_commands_total")

    async def coordinate(self, agent_id, step=None, intent=None, show_details=False):
        gate = await self.validate(agent_id, "coordinate")
        if gate:
            return gate
        current = await self.store.get_session(agent_id)
        if current is None:
            return self.expired_result(agent_id)
        details = current["details"]
        if intent is not None and step is None:
            return {
                "ok": False,
                "agent_name": public_agent_name(agent_id),
                "error": "coordinate.step: step is required when intent is provided",
            }
        requested_step = current["current_step"] if step is None else step
        if requested_step < 1 or requested_step > len(details):
            return {
                "ok": False,
                "agent_name": public_agent_name(agent_id),
                "error": f"coordinate.step: must be between 1 and {len(details)}",
            }
        if intent is not None:
            now = utc_text()
            await self.store.update_coordinate(agent_id, intent, requested_step, now)
            self._inc("terminal_mcp_agent_task_updates_total")
            current = await self.store.get_session(agent_id)

        other_details = []
        if show_details:
            cutoff = utc_text(utc_now() - timedelta(seconds=self.ttl_seconds))
            sessions = await self.store.active(cutoff, MAX_ACTIVE_AGENTS + 1)
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

        return {
            "ok": True,
            "agent_name": public_agent_name(agent_id),
            "step": requested_step,
            "intent": current["intent"],
            "detail": details[requested_step - 1],
            "other_details": other_details,
            "active_agents": await self.active_snapshot(exclude_agent_id=agent_id),
        }

    async def message(self, agent_id, text=None, target=None, message_hash=None):
        gate = await self.validate(agent_id, "message")
        if gate:
            return gate

        if message_hash is not None:
            if text is not None or target is not None:
                return {
                    "ok": False,
                    "agent_name": public_agent_name(agent_id),
                    "error": "message.mode: acknowledgement cannot include text or target",
                }
            acknowledged = await self.store.acknowledge_message(
                message_hash, agent_id, utc_text()
            )
            if not acknowledged:
                return {
                    "ok": False,
                    "agent_name": public_agent_name(agent_id),
                    "message_hash": message_hash,
                    "error": "message.ack: caller is neither sender nor recipient",
                }
            readers = await self.store.message_readers(message_hash)
            return {
                "ok": True,
                "agent_name": public_agent_name(agent_id),
                "message_hash": message_hash,
                "read_by": [public_agent_name(value) for value in readers],
                "delivered_to": [],
            }

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
        sessions = await self.store.active(cutoff, MAX_ACTIVE_AGENTS + 1)
        recipients = [item for item in sessions if item["agent_id"] != agent_id]
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

        created_at = utc_text()
        for _ in range(32):
            allocated_hash = secrets.token_hex(4)
            try:
                await self.store.create_message(
                    allocated_hash,
                    agent_id,
                    target,
                    text,
                    created_at,
                    [item["agent_id"] for item in recipients],
                )
                break
            except IntegrityError:
                continue
        else:
            raise RuntimeError("unable to allocate unique message hash")

        self._inc("terminal_mcp_coordination_messages_total")
        return {
            "ok": True,
            "agent_name": public_agent_name(agent_id),
            "message_hash": allocated_hash,
            "delivered_to": [public_agent_name(item["agent_id"]) for item in recipients],
            "read_by": [],
        }

    async def finish(self, agent_id):
        gate = await self.validate(agent_id, "agent_finish")
        if gate:
            return gate
        await self.store.finish(agent_id)
        return {"ok": True, "agent_name": public_agent_name(agent_id), "finished": True}

    async def overview(self, agent_id, touch=True, reveal_self_id=False):
        if touch:
            gate = await self.validate(agent_id, "agents")
            if gate:
                return gate
        now = utc_now()
        cutoff = utc_text(now - timedelta(seconds=self.ttl_seconds))
        sessions = await self.store.active(cutoff, MAX_ACTIVE_AGENTS + 1)
        own = next(
            (session for session in sessions if session["agent_id"] == agent_id),
            await self.store.get_session(agent_id),
        )
        others = [
            session for session in sessions if session["agent_id"] != agent_id
        ][:MAX_ACTIVE_AGENTS]
        active = []
        for session in others:
            idle = max(
                0, int((now - parse_utc(session["last_activity_at"])).total_seconds())
            )
            active.append(
                {
                    "name": public_agent_name(session["agent_id"]),
                    "idle_seconds": idle,
                    "task_summary": session["task_summary"],
                    "intent": session["intent"],
                    "work_scope": session["work_scope"],
                    "current_step": session["current_step"],
                    "recent_commands": await self.store.recent_commands(
                        session["agent_id"], MAX_RECENT_COMMANDS
                    ),
                }
            )
        total = await self.store.count_active(cutoff)
        overlaps = find_scope_overlaps(own["work_scope"] if own else [], active)
        if overlaps:
            self._inc("terminal_mcp_agent_scope_overlaps_total", len(overlaps))
        self._set("terminal_mcp_active_agents", total)
        self_payload = {
            "name": public_agent_name(agent_id),
            "ttl_seconds": self.ttl_seconds,
            "task_lease_seconds": self.task_lease_seconds,
            "task_summary": own["task_summary"] if own else "",
            "intent": own["intent"] if own else "",
            "work_scope": own["work_scope"] if own else [],
            "details": own["details"] if own else [],
            "current_step": own["current_step"] if own else 1,
        }
        if reveal_self_id:
            self_payload["agent_id"] = agent_id
        return {
            "ok": True,
            "self": self_payload,
            "active": active,
            "overlaps": overlaps,
            "additional_active_agents": max(0, total - 1 - len(active)),
        }

    @staticmethod
    def expired_result(agent_id):
        return {
            "ok": False,
            "agent_name": public_agent_name(agent_id),
            "session_expired": True,
            "registration_required": True,
        }

    def _inc(self, name, amount=1):
        if self.metrics:
            self.metrics.inc(name, value=amount)

    def _set(self, name, value):
        if self.metrics:
            self.metrics.set(name, value)
