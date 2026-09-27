from __future__ import annotations

import secrets
from collections import Counter

from terminal_mcp.core.orchestration import parse_utc, public_agent_name, utc_now, utc_text
from terminal_mcp.storage.tasks import TaskRevisionConflict

LANES = ("implementation", "review", "release", "integration", "general")
STATES = ("ready", "blocked", "deferred", "done")
PRIORITIES = ("P0", "P1", "P2", "P3")
REVIEW_DIMENSIONS = ("A", "C", "R")
PRIORITY_VALUE = {"P0": 3, "P1": 2, "P2": 1, "P3": 0}
VALUE_PRIORITY = {value: key for key, value in PRIORITY_VALUE.items()}
PRESSURE_WEIGHT = {"P0": 8, "P1": 4, "P2": 2, "P3": 1}


def _warning(code: str, message: str, *, task_id=None, severity="warning", **context):
    item = {"code": code, "severity": severity, "message": message}
    if task_id:
        item["task_id"] = task_id
    if context:
        item["context"] = context
    return item


class TaskCoordinator:
    """Optional task workflow with soft guardrails and durable evidence."""

    def __init__(
        self,
        store,
        agent_store=None,
        metrics=None,
        *,
        session_ttl_seconds=300,
        max_session_seconds=1500,
    ):
        self.store = store
        self.agent_store = agent_store
        self.metrics = metrics
        self.session_ttl_seconds = session_ttl_seconds
        self.max_session_seconds = max_session_seconds

    def _inc(self, name, labels=()):
        if self.metrics:
            self.metrics.inc(name, labels)

    async def _live_claims(self, namespace, task_id):
        claims = await self.store.active_claims(namespace, task_id)
        if not self.agent_store:
            return claims
        now = utc_now()
        live = []
        for item in claims:
            session = await self.agent_store.get_session(item["agent_id"])
            if not session or session["state"] != "active":
                continue
            idle = (now - parse_utc(session["last_activity_at"])).total_seconds()
            age = (now - parse_utc(session["registered_at"])).total_seconds()
            if idle >= self.session_ttl_seconds or age >= self.max_session_seconds:
                continue
            live.append(item)
        return live

    async def _dependencies(self, namespace, task_id):
        result = []
        for item in await self.store.dependencies(namespace, task_id):
            dep = await self.store.get_task(item["namespace"], item["task_id"])
            result.append(
                {
                    "namespace": item["namespace"],
                    "task_id": item["task_id"],
                    "state": dep["state"] if dep else "missing",
                }
            )
        return result

    def _external_task(self, task):
        result = dict(task)
        result["priority"] = VALUE_PRIORITY.get(int(task.get("priority", 0)), "P3")
        result["resource_context"] = result.pop("resource", {})
        result["review_requirements"] = result.pop("reviews", [])
        return result

    async def _decorate(self, task, *, details=False):
        result = self._external_task(task)
        namespace, task_id = result["namespace"], result["task_id"]
        claims = await self._live_claims(namespace, task_id)
        result["claims"] = [
            {"agent_name": public_agent_name(item["agent_id"]), "claimed_at": item["claimed_at"]}
            for item in claims
        ]
        result["active"] = bool(claims)
        if details:
            result["dependencies"] = await self._dependencies(namespace, task_id)
            reviews = await self.store.reviews(namespace, task_id)
            for review in reviews:
                review["agent_name"] = public_agent_name(review.pop("agent_id"))
            result["reviews"] = reviews
            events = await self.store.list_events(namespace, task_id, limit=50)
            for event in events:
                if event.get("agent_id"):
                    event["agent_name"] = public_agent_name(event.pop("agent_id"))
            result["events"] = events
        else:
            result.pop("description", None)
            result.pop("resource_context", None)
        return result

    async def list(
        self,
        *,
        namespace=None,
        task_id=None,
        lane=None,
        state=None,
        show_details=False,
        show_done=False,
        limit=50,
        cursor=None,
    ):
        if lane is not None and lane not in LANES:
            return {"ok": False, "error": f"tasks.lane: expected one of {', '.join(LANES)}"}
        if state is not None and state not in STATES:
            return {"ok": False, "error": f"tasks.state: expected one of {', '.join(STATES)}"}
        if task_id:
            if not namespace:
                return {"ok": False, "error": "tasks.namespace: required with task_id"}
            task = await self.store.get_task(namespace, task_id)
            return {
                "ok": task is not None,
                "task": await self._decorate(task, details=show_details) if task else None,
                "error": None if task else "task not found",
            }

        offset = max(0, int(cursor or 0))
        rows = await self.store.list_tasks(
            namespace=namespace,
            lane=lane,
            state=state,
            show_done=show_done,
            limit=limit,
            offset=offset,
        )
        tasks = [await self._decorate(item, details=show_details) for item in rows]
        lane_counts = Counter(item["lane"] for item in tasks if item["state"] != "done")
        state_counts = Counter(item["state"] for item in tasks)
        pressure = Counter()
        recommended = None
        for item in tasks:
            if item["state"] != "ready":
                continue
            pressure[item["lane"]] += PRESSURE_WEIGHT[item["priority"]]
            if recommended is None and not item["claims"]:
                deps = await self._dependencies(item["namespace"], item["task_id"])
                if not any(dep["state"] != "done" for dep in deps):
                    recommended = {
                        "namespace": item["namespace"],
                        "task_id": item["task_id"],
                        "lane": item["lane"],
                        "priority": item["priority"],
                        "title": item["title"],
                    }
        return {
            "ok": True,
            "summary": {
                "visible": len(tasks),
                "by_lane": dict(lane_counts),
                "by_state": dict(state_counts),
                "pressure": dict(pressure),
            },
            "recommended": recommended,
            "tasks": tasks,
            "next_cursor": offset + len(rows) if len(rows) == limit else None,
        }

    async def mutate(self, agent_id, *, action, namespace, task_id=None, **kwargs):
        if not namespace:
            return {"ok": False, "error": "task.namespace: required", "warnings": []}
        if self.agent_store:
            session = await self.agent_store.get_session(agent_id)
            if not session or session["state"] != "active":
                return {
                    "ok": False,
                    "error": "task.agent_id: active session required",
                    "warnings": [],
                }
        handler = getattr(self, f"_action_{action}", None)
        if handler is None:
            return {
                "ok": False,
                "error": f"task.action: unsupported action {action}",
                "warnings": [],
            }
        return await handler(agent_id, namespace, task_id, **kwargs)

    async def _action_create(self, agent_id, namespace, task_id, **kwargs):
        dependency_error = self._validate_dependencies(kwargs.get("dependencies"))
        if dependency_error:
            return {"ok": False, "error": dependency_error, "warnings": []}
        task_id = task_id or f"TASK-{secrets.token_hex(3).upper()}"
        lane = kwargs.get("lane", "general")
        priority = kwargs.get("priority", "P2")
        state = kwargs.get("state", "ready")
        dimensions = kwargs.get("review_requirements") or []
        error = self._validate(lane=lane, priority=priority, state=state, dimensions=dimensions)
        if error:
            return {"ok": False, "error": error, "warnings": []}
        now = utc_text()
        try:
            await self.store.create_task_mutation(
                namespace,
                task_id,
                kwargs.get("title") or task_id,
                lane=lane,
                priority=PRIORITY_VALUE[priority],
                state=state,
                description=kwargs.get("description") or "",
                next_action=kwargs.get("next_action") or "",
                resource=kwargs.get("resource_context") or {},
                reviews=dimensions,
                cooperative=bool(kwargs.get("cooperative")),
                checkpoint=kwargs.get("checkpoint") or {},
                candidate_ref=kwargs.get("candidate_ref"),
                dependencies=kwargs.get("dependencies"),
                event_agent_id=agent_id,
                event_payload={"lane": lane, "priority": priority, "state": state},
                now=now,
            )
        except Exception as exc:
            return {"ok": False, "error": f"task.create: {exc}", "warnings": []}
        self._inc("terminal_mcp_tasks_created_total")
        return await self._result(namespace, task_id, [])

    async def _action_claim(self, agent_id, namespace, task_id, **kwargs):
        current = await self._required(namespace, task_id)
        if not current:
            return self._missing()
        warnings = []
        others = [
            item
            for item in await self._live_claims(namespace, task_id)
            if item["agent_id"] != agent_id
        ]
        if others:
            warnings.append(
                _warning(
                    "already_claimed",
                    "Task already has active claims; concurrent work is visible and allowed.",
                    task_id=task_id,
                    severity="info" if current.get("cooperative") else "warning",
                    claims=[public_agent_name(item["agent_id"]) for item in others],
                    cooperative=bool(current.get("cooperative")),
                )
            )
        if current["state"] in {"blocked", "deferred", "done"}:
            warnings.append(
                _warning(
                    f"{current['state']}_task",
                    f"Task state is {current['state']}.",
                    task_id=task_id,
                )
            )
        open_deps = [
            dep for dep in await self._dependencies(namespace, task_id) if dep["state"] != "done"
        ]
        if open_deps:
            warnings.append(
                _warning(
                    "dependency_open",
                    "Task has unfinished dependencies.",
                    task_id=task_id,
                    dependencies=open_deps,
                )
            )
        now = utc_text()
        claim = await self.store.claim(namespace, task_id, agent_id, now=now)
        await self.store.add_event(
            namespace,
            task_id,
            "claim" if claim.get("created") else "claim_refresh",
            agent_id=agent_id,
            payload={"warnings": [item["code"] for item in warnings]},
            now=now,
        )
        self._inc("terminal_mcp_task_claims_total")
        return await self._result(namespace, task_id, warnings)

    async def _action_release(self, agent_id, namespace, task_id, **kwargs):
        if not await self._required(namespace, task_id):
            return self._missing()
        now = utc_text()
        released = await self.store.release_claim(namespace, task_id, agent_id, now=now)
        if released:
            await self.store.add_event(
                namespace, task_id, "claim_released", agent_id=agent_id, now=now
            )
        return await self._result(namespace, task_id, [])

    async def _action_checkpoint(self, agent_id, namespace, task_id, **kwargs):
        if not await self._required(namespace, task_id):
            return self._missing()
        if "checkpoint" not in kwargs:
            return {"ok": False, "error": "task.checkpoint: checkpoint required", "warnings": []}
        return await self._update(
            agent_id,
            namespace,
            task_id,
            {"checkpoint": kwargs["checkpoint"]},
            kwargs.get("expected_revision"),
            "checkpoint",
        )

    async def _action_update(self, agent_id, namespace, task_id, **kwargs):
        if not await self._required(namespace, task_id):
            return self._missing()
        return await self._update_from_kwargs(agent_id, namespace, task_id, kwargs)

    async def _action_state(self, agent_id, namespace, task_id, **kwargs):
        if kwargs.get("state") is None:
            return {"ok": False, "error": "task.state: state required", "warnings": []}
        return await self._action_update(agent_id, namespace, task_id, **kwargs)

    async def _action_done(self, agent_id, namespace, task_id, **kwargs):
        kwargs["state"] = "done"
        return await self._action_update(agent_id, namespace, task_id, **kwargs)

    async def _update_from_kwargs(self, agent_id, namespace, task_id, kwargs):
        dependency_error = self._validate_dependencies(kwargs.get("dependencies"))
        if dependency_error:
            return {"ok": False, "error": dependency_error, "warnings": []}
        current = await self.store.get_task(namespace, task_id)
        fields = {}
        mapping = {
            "title": "title",
            "lane": "lane",
            "state": "state",
            "description": "description",
            "next_action": "next_action",
            "resource_context": "resource",
            "review_requirements": "reviews",
            "cooperative": "cooperative",
            "checkpoint": "checkpoint",
            "candidate_ref": "candidate_ref",
        }
        for external, internal in mapping.items():
            if kwargs.get(external) is not None:
                fields[internal] = kwargs[external]
        if kwargs.get("priority") is not None:
            fields["priority"] = PRIORITY_VALUE.get(kwargs["priority"], -1)
        error = self._validate(
            lane=kwargs.get("lane"),
            priority=kwargs.get("priority"),
            state=kwargs.get("state"),
            dimensions=kwargs.get("review_requirements"),
        )
        if error:
            return {"ok": False, "error": error, "warnings": []}
        warnings = []
        target_state = kwargs.get("state")
        if target_state == "done":
            required = set(current.get("reviews") or [])
            candidate = current.get("candidate_ref")
            approvals = (
                await self.store.reviews(namespace, task_id, candidate_ref=candidate)
                if candidate
                else []
            )
            approved = {
                item["dimension"] for item in approvals if item["verdict"] == "NON_BLOCKING"
            }
            missing = sorted(required - approved)
            if missing:
                warnings.append(
                    _warning(
                        "review_incomplete",
                        "Task is being completed without all requested review dimensions.",
                        task_id=task_id,
                        dimensions=missing,
                    )
                )
        if current["state"] == "blocked" and target_state not in {None, "blocked"}:
            warnings.append(
                _warning(
                    "unusual_transition",
                    "Blocked task state is being changed explicitly.",
                    task_id=task_id,
                    from_state="blocked",
                    to_state=target_state,
                )
            )
        return await self._update(
            agent_id,
            namespace,
            task_id,
            fields,
            kwargs.get("expected_revision"),
            "updated",
            warnings=warnings,
            dependencies=kwargs.get("dependencies"),
        )

    async def _update(
        self,
        agent_id,
        namespace,
        task_id,
        fields,
        expected_revision,
        event_type,
        warnings=None,
        dependencies=None,
    ):
        warnings = list(warnings or [])
        now = utc_text()
        event_payload = {
            "fields": sorted(fields),
            "warnings": [item["code"] for item in warnings],
        }
        for key in ("checkpoint", "candidate_ref", "state", "lane", "priority", "next_action"):
            if key in fields:
                event_payload[key] = fields[key]
        try:
            await self.store.update_task_mutation(
                namespace,
                task_id,
                expected_revision=expected_revision,
                dependencies=dependencies,
                event_type=event_type,
                event_agent_id=agent_id,
                event_payload=event_payload,
                now=now,
                **fields,
            )
        except TaskRevisionConflict as exc:
            warnings.append(
                _warning(
                    "concurrent_update",
                    "Task revision changed before update was applied.",
                    task_id=task_id,
                    expected=exc.expected,
                    actual=exc.actual,
                )
            )
            return await self._result(
                namespace, task_id, warnings, ok=False, error="revision conflict"
            )
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": f"task.update: {exc}", "warnings": warnings}
        except Exception as exc:
            return {"ok": False, "error": f"task.update: {exc}", "warnings": warnings}
        return await self._result(namespace, task_id, warnings)

    async def _action_review(self, agent_id, namespace, task_id, **kwargs):
        current = await self._required(namespace, task_id)
        if not current:
            return self._missing()
        dimensions = kwargs.get("dimensions") or []
        verdict = kwargs.get("verdict")
        error = self._validate(dimensions=dimensions)
        if error:
            return {"ok": False, "error": error, "warnings": []}
        if not dimensions or verdict not in {"NON_BLOCKING", "BLOCKING"}:
            return {
                "ok": False,
                "error": "task.review: dimensions and verdict=NON_BLOCKING|BLOCKING required",
                "warnings": [],
            }
        warnings = []
        candidate = kwargs.get("candidate_ref") or current.get("candidate_ref")
        if not candidate:
            warnings.append(
                _warning(
                    "stale_candidate",
                    "Review has no candidate reference to bind the verdict.",
                    task_id=task_id,
                )
            )
        elif current.get("candidate_ref") and candidate != current["candidate_ref"]:
            warnings.append(
                _warning(
                    "stale_candidate",
                    "Review candidate differs from task current candidate.",
                    task_id=task_id,
                    current=current["candidate_ref"],
                    reviewed=candidate,
                )
            )
        history = await self.store.claims(namespace, task_id, active_only=False)
        if any(item["agent_id"] == agent_id for item in history):
            warnings.append(
                _warning(
                    "self_review",
                    "Reviewer has task claim history for this task.",
                    task_id=task_id,
                    agent_name=public_agent_name(agent_id),
                )
            )
        now = utc_text()
        for dimension in dimensions:
            await self.store.upsert_review(
                namespace,
                task_id,
                candidate_ref=candidate,
                dimension=dimension,
                verdict=verdict,
                agent_id=agent_id,
                evidence=kwargs.get("evidence") or {},
                warnings=warnings,
                now=now,
            )
        await self.store.add_event(
            namespace,
            task_id,
            "review",
            agent_id=agent_id,
            payload={
                "candidate_ref": candidate,
                "dimensions": dimensions,
                "verdict": verdict,
                "evidence": kwargs.get("evidence") or {},
                "warnings": [item["code"] for item in warnings],
            },
            now=now,
        )
        self._inc("terminal_mcp_task_reviews_total")
        return await self._result(namespace, task_id, warnings)

    async def _required(self, namespace, task_id):
        return await self.store.get_task(namespace, task_id) if task_id else None

    @staticmethod
    def _missing():
        return {"ok": False, "error": "task not found", "warnings": []}

    @staticmethod
    def _validate_dependencies(dependencies):
        if dependencies is None:
            return None
        for index, item in enumerate(dependencies):
            if not isinstance(item, dict):
                return f"task.dependencies[{index}]: expected object"
            task_id = item.get("task_id")
            namespace = item.get("namespace")
            if not isinstance(task_id, str) or not task_id.strip():
                return f"task.dependencies[{index}].task_id: required"
            if namespace is not None and (not isinstance(namespace, str) or not namespace.strip()):
                return f"task.dependencies[{index}].namespace: expected non-empty string"
        return None

    def _validate(self, *, lane=None, priority=None, state=None, dimensions=None):
        if lane is not None and lane not in LANES:
            return f"task.lane: expected one of {', '.join(LANES)}"
        if priority is not None and priority not in PRIORITIES:
            return f"task.priority: expected one of {', '.join(PRIORITIES)}"
        if state is not None and state not in STATES:
            return f"task.state: expected one of {', '.join(STATES)}"
        if dimensions is not None:
            invalid = [item for item in dimensions if item not in REVIEW_DIMENSIONS]
            if invalid:
                return "task.review_dimensions: expected A, C and/or R"
        return None

    async def health(self):
        stats = await self.store.stats()
        stale_claims = 0
        now = utc_now()
        for claim in await self.store.all_active_claims():
            session = (
                await self.agent_store.get_session(claim["agent_id"]) if self.agent_store else None
            )
            if not session or session["state"] != "active":
                stale_claims += 1
                continue
            idle = (now - parse_utc(session["last_activity_at"])).total_seconds()
            age = (now - parse_utc(session["registered_at"])).total_seconds()
            if idle >= self.session_ttl_seconds or age >= self.max_session_seconds:
                stale_claims += 1
        stats["stale_claims"] = stale_claims
        stats["ok"] = True
        if self.metrics:
            for state, count in stats["by_state"].items():
                self.metrics.set("terminal_mcp_tasks", count, (("state", state),))
            for lane, count in stats["by_lane"].items():
                self.metrics.set("terminal_mcp_tasks_by_lane", count, (("lane", lane),))
            self.metrics.set("terminal_mcp_task_active_claims", stats["active_claims"])
            self.metrics.set("terminal_mcp_task_stale_claims", stale_claims)
        return stats

    async def release_agent_claims(self, agent_id, *, reason="agent_finish", now=None):
        now = now or utc_text()
        claims = await self.store.claims_for_agent(agent_id, active_only=True)
        if not claims:
            return 0
        released = 0
        for claim in claims:
            if not await self.store.release_claim(
                claim["namespace"], claim["task_id"], agent_id, now=now
            ):
                continue
            released += 1
            await self.store.add_event(
                claim["namespace"],
                claim["task_id"],
                "claim_released",
                agent_id=agent_id,
                payload={"reason": reason},
                now=now,
            )
        return released

    async def task_refs_for_agent(self, agent_id):
        rows = await self.store.claims_for_agent(agent_id, active_only=True)
        return [
            {
                "namespace": item["namespace"],
                "task_id": item["task_id"],
                "lane": item["lane"],
                "priority": VALUE_PRIORITY.get(int(item["priority"]), "P3"),
                "state": item["state"],
            }
            for item in rows
        ]

    async def record_command(self, agent_id, command_hash, command_type):
        now = utc_text()
        for item in await self.store.claims_for_agent(agent_id, active_only=True):
            await self.store.add_event(
                item["namespace"],
                item["task_id"],
                "command",
                agent_id=agent_id,
                payload={"command_hash": command_hash, "command_type": command_type},
                now=now,
            )

    async def _result(self, namespace, task_id, warnings, *, ok=True, error=None):
        task = await self.store.get_task(namespace, task_id)
        for warning in warnings:
            self._inc("terminal_mcp_task_warnings_total", (("code", warning["code"]),))
        return {
            "ok": ok,
            "task": await self._decorate(task, details=False) if task else None,
            "warnings": warnings,
            "error": error,
        }
