from __future__ import annotations

from datetime import timedelta

from terminal_mcp.core.agent_policy import AgentPolicy
from terminal_mcp.core.orchestration import (
    find_scope_overlaps,
    parse_utc,
    public_agent_name,
    public_session_ref,
    relative_time,
    utc_now,
    utc_text,
)


class AgentHistory:
    """Read preserved session history; no registration or authorization lifecycle."""

    def __init__(self, store, policy=None, task_store=None, local_instance_id=None):
        self.store = store
        self.policy = policy or AgentPolicy()
        self.task_store = task_store
        self.local_instance_id = local_instance_id
        self.ttl_seconds = self.policy.idle_ttl_seconds
        self.task_context_ttl_seconds = self.policy.intent_ttl_seconds
        self.max_session_seconds = self.policy.max_session_seconds

    async def active_snapshot(self, exclude_agent_id=None):
        result = await self.overview()
        return [item for item in result["active"] if item.get("agent_id") != exclude_agent_id]

    def _intent_scope_views(self, session, now):
        views = []
        local_status = "missing" if self.local_instance_id else None
        for instance_id, item in sorted((session.get("intent_scopes") or {}).items()):
            updated_at = item["updated_at"]
            age_seconds = max(0, int((now - parse_utc(updated_at)).total_seconds()))
            status = "fresh" if age_seconds <= self.task_context_ttl_seconds else "stale"
            local = bool(self.local_instance_id and instance_id == self.local_instance_id)
            if local:
                local_status = status
            views.append(
                {
                    "instance_id": instance_id,
                    "intent": item["intent"],
                    "current_step": int(item["current_step"]),
                    "updated_at": updated_at,
                    "age_seconds": age_seconds,
                    "status": status,
                    "local": local,
                }
            )
        return views, local_status

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
            "mode": (
                item.get("delivery_mode")
                if item.get("delivery_mode") in {"notify", "ack", "alert"}
                else "alert"
                if item["alert"]
                else "legacy"
            ),
            "namespace": item.get("task_namespace"),
            "task_id": item.get("task_id"),
            "created_at": item["created_at"],
            "first_seen_at": item["first_seen_at"],
            "read_at": item["read_at"],
            "replied_at": item["replied_at"],
            "seen_count": item["seen_count"],
        }

    async def _managed_task_refs(self, agent_id):
        if self.task_store is None:
            return []
        priorities = {3: "P0", 2: "P1", 1: "P2", 0: "P3"}
        rows = await self.task_store.historical_claims_for_agent(agent_id, active_only=True)
        result = []
        for item in rows:
            claims = [
                claim
                for claim in await self.task_store.claims(
                    item["namespace"], item["task_id"], active_only=False
                )
                if claim["owner_kind"] == "legacy_session" and claim["released_at"] is None
            ]
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
        target_agent_id=None,
        target_session_ref=None,
        show_details=False,
        show_intents=False,
        show_commands=False,
        command_hash=None,
        since_minutes=None,
        touch=True,
        reveal_self_id=False,
        reveal_agent_ids=False,
    ):
        caller_context = {}
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
        if target_agent_id:
            sessions = [s for s in sessions if s["agent_id"] == target_agent_id]
            sessions = sessions[:1]
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
        if target or target_agent_id or target_session_ref:
            sessions = sessions[:1]
        targeted = bool(target or target_agent_id or target_session_ref)

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
            intent_scopes, local_intent_status = self._intent_scope_views(session, now)
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
                "logical_last_activity_at": session["last_activity_at"],
                "logical_idle_seconds": idle_seconds,
                "logical_session_status": (
                    "active" if session["state"] == "active" else session["state"]
                ),
                "idle_seconds": idle_seconds,
                "session_age_seconds": session_age_seconds,
                "intent": session["intent"],
                "current_step": session["current_step"],
                "end_reason": session["end_reason"],
            }
            if intent_scopes:
                record["intent_scopes"] = intent_scopes
            if local_intent_status is not None:
                record["local_intent_status"] = local_intent_status
            if reveal_agent_ids:
                record["agent_id"] = session["agent_id"]
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
            if targeted:
                message_journal = await self.store.message_journal(session["agent_id"], cutoff)
                record["message_journal"] = [
                    self._message_obligation_record(item) for item in message_journal
                ]
            if show_commands or targeted:
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
        for record, session in zip(records, sessions, strict=True):
            if record["status"] in {"started", "active", "idle"}:
                idle = max(0, int((now - parse_utc(session["last_activity_at"])).total_seconds()))
                if agent_id and session["agent_id"] == agent_id:
                    continue
                active_record = {
                    "name": record["name"],
                    "status": record["status"],
                    "logical_session_status": record["logical_session_status"],
                    "session_age_seconds": record["session_age_seconds"],
                    "idle_seconds": idle,
                    "logical_idle_seconds": record["logical_idle_seconds"],
                    "logical_last_activity_at": record["logical_last_activity_at"],
                    "intent": session["intent"],
                    "current_step": session["current_step"],
                    "intent_scopes": record.get("intent_scopes", []),
                    "local_intent_status": record.get("local_intent_status"),
                }
                if reveal_agent_ids:
                    active_record["agent_id"] = session["agent_id"]
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
            own_scopes, own_local_intent_status = self._intent_scope_views(own, now)
            own_idle = max(0, int((now - parse_utc(own["last_activity_at"])).total_seconds()))
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
                "logical_session_status": ("active" if own["state"] == "active" else own["state"]),
                "logical_last_activity_at": own["last_activity_at"],
                "logical_idle_seconds": own_idle,
                "intent_scopes": own_scopes,
                "local_intent_status": own_local_intent_status,
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
