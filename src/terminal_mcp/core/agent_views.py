"""Current Console agent views backed by the native ACS attachment registry."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from terminal_mcp.core.agent_history import AgentHistory
from terminal_mcp.core.orchestration import parse_utc, relative_time, utc_now, utc_text
from terminal_mcp.core.persistent_agents import ClaimOwner


class AgentViews:
    def __init__(self, service):
        self.service = service

    async def active_snapshot(self, exclude_agent_id=None):
        result = await self.overview()
        return [row for row in result["active"] if row["agent_id"] != exclude_agent_id]

    async def _task_refs(self, agent_id, now):
        store = self.service.task_store
        if store is None:
            return []
        claims = await store.claims_for_owner(ClaimOwner.logical_agent(agent_id))
        return [
            {
                "namespace": claim["namespace"],
                "task_id": claim["task_id"],
                "lane": claim["lane"],
                "priority": {3: "P0", 2: "P1", 1: "P2", 0: "P3"}.get(claim["priority"], "P3"),
                "state": claim["state"],
                "operational_status": (
                    "in_progress" if claim["state"] == "ready" else claim["state"]
                ),
                "isolation_hint": claim["isolation_hint"],
                "claimed_at": claim["claimed_at"],
                "claim_age_seconds": max(
                    0, int((now - parse_utc(claim["claimed_at"])).total_seconds())
                ),
                "claim_intent": claim["claim_intent"] or "",
                "role": "owner",
            }
            for claim in claims
        ]

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
        history=False,
        **_read_options,
    ):
        # History is an explicit read path and never supplies live identities.
        if history or command_hash:
            result = await self.service.agent_history.overview(
                agent_id=agent_id,
                target=target,
                target_agent_id=target_agent_id,
                target_session_ref=target_session_ref,
                show_details=show_details,
                show_intents=show_intents,
                show_commands=show_commands,
                command_hash=command_hash,
                since_minutes=since_minutes,
                reveal_agent_ids=True,
            )
            result.update(active=[], additional_active_agents=0, self=None)
            return result

        mesh = getattr(self.service, "access_mesh", None)
        if mesh is None:
            return {"ok": True, "active": [], "sessions": [], "additional_active_agents": 0}
        store = self.service.agent_store
        now = utc_now()
        policy = self.service.agent_policy
        cutoff = utc_text(now - timedelta(minutes=since_minutes or policy.history_default_minutes))
        page = await asyncio.to_thread(
            mesh.store.agent_overview,
            now=now,
            include_inactive=True,
            logical_agent_id=target_agent_id,
            limit=100,
        )
        selected = [
            row
            for row in page.agents
            if (not target or row["public_name"] == target)
            and (not target_session_ref or row["work_session_id"] == target_session_ref)
            and (not row["hard_expires_at"] or row["hard_expires_at"] >= cutoff)
        ]
        targeted = bool(target or target_agent_id or target_session_ref)
        if targeted:
            selected = selected[:1]
        read_limit = asyncio.Semaphore(8)

        async def record(row):
            async with read_limit:
                identifier = row["logical_agent_id"]
                activity_at = row["last_activity"] or row["session_started_at"] or utc_text(now)
                started = row["session_started_at"] or activity_at
                live = row["session_state"] in {"active", "warning", "draining"}
                obligations, activity = await asyncio.gather(
                    store.message_obligations(identifier), store.latest_activity(identifier)
                )
                value = {
                    "name": row["public_name"],
                    "agent_id": identifier,
                    "logical_agent_id": identifier,
                    "work_session_id": row["work_session_id"],
                    "session_ref": row["work_session_id"],
                    "session_epoch": row["session_epoch"],
                    "origin_instance_id": row["last_server"],
                    "session_started_at": started,
                    "session_age_seconds": max(0, int((now - parse_utc(started)).total_seconds())),
                    "session_remaining_seconds": max(
                        0, int((parse_utc(row["hard_expires_at"]) - now).total_seconds())
                    )
                    if live and row["hard_expires_at"]
                    else 0,
                    "status": row["session_state"],
                    "last_activity": relative_time(activity_at, now),
                    "last_activity_at": activity_at,
                    "idle_seconds": max(0, int((now - parse_utc(activity_at)).total_seconds())),
                    "last_activity_tool": activity["tool"] if activity else None,
                    "last_activity_command_hash": activity["command_hash"] if activity else None,
                    "intent": row["current_intent"] or "",
                    "intent_state": row["intent_state"],
                    "current_step": 0,
                    "messages_awaiting_read": sum(item["read_at"] is None for item in obligations),
                    "messages_awaiting_reply": sum(
                        bool(item["require_reply"] and item["replied_at"] is None)
                        for item in obligations
                    ),
                    "alerts_pending": sum(
                        bool(item["alert"] and item["replied_at"] is None) for item in obligations
                    ),
                }
                if targeted:
                    journal = await store.message_journal(identifier, cutoff)
                    value["message_journal"] = [
                        AgentHistory._message_obligation_record(item) for item in journal
                    ]
                if show_commands or targeted:
                    commands = await store.recent_commands(identifier, 3)
                    value["last_command"] = commands[0] if commands else None
                    value["recent_commands"] = commands
                if show_details:
                    value.update(
                        task_summary=row["current_intent"] or "",
                        work_scope=[],
                        details=[],
                        registered_at=started,
                        ended_at=None if live else row["hard_expires_at"],
                    )
                if live:
                    value["managed_tasks"] = await self._task_refs(identifier, now)
                return value

        records = list(await asyncio.gather(*(record(row) for row in selected)))
        active = [
            row
            for row in records
            if row["status"] in {"active", "warning", "draining"} and row["agent_id"] != agent_id
        ]
        own = next((row for row in records if row["agent_id"] == agent_id), None)
        self_payload = None
        if own:
            self_payload = {
                "name": own["name"],
                "agent_id": agent_id,
                "ttl_seconds": policy.idle_ttl_seconds,
                "task_context_ttl_seconds": 180,
                "task_lease_seconds": 180,
                "task_summary": own["intent"],
                "intent": own["intent"],
                "current_step": 0,
                "managed_tasks": own.get("managed_tasks", []),
            }
        chosen = selected[0] if selected else None
        intents = []
        if chosen and show_intents:
            intents = await store.intent_journal(chosen["logical_agent_id"], cutoff)
            if chosen["current_intent"] and chosen["intent_updated_at"]:
                current = {
                    "timestamp": chosen["intent_updated_at"],
                    "intent": chosen["current_intent"],
                    "step": 0,
                    "work_scope": [],
                }
                if not any(item["timestamp"] == current["timestamp"] for item in intents):
                    intents.append(current)
        commands = (
            await store.command_journal(chosen["logical_agent_id"], cutoff)
            if chosen and show_commands
            else []
        )
        return {
            "ok": True,
            "self": self_payload,
            "active": active[: policy.max_active_agents],
            "sessions": records,
            "intent_journal": intents or None,
            "command_journal": commands or None,
            "additional_active_agents": max(0, len(active) - policy.max_active_agents),
        }
