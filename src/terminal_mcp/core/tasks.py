from __future__ import annotations

import secrets
from collections import Counter

from terminal_mcp.core.orchestration import (
    live_task_claims,
    parse_utc,
    public_agent_name,
    session_is_live,
    utc_now,
    utc_text,
)
from terminal_mcp.storage.tasks import TaskClaimConflict, TaskRevisionConflict

LANES = ("implementation", "review", "release", "integration", "general")
STATES = ("ready", "blocked", "deferred", "done")
OPERATIONAL_STATUSES = ("ready", "in_progress", "blocked", "deferred", "done")
PRIORITIES = ("P0", "P1", "P2", "P3")
ACTIONS = (
    "create",
    "claim",
    "release",
    "update",
    "checkpoint",
    "state",
    "done",
    "archive",
    "comment",
    "relate",
    "unrelate",
)
REVIEW_DIMENSIONS = ("A", "C", "R")  # legacy read-only review history
PRIORITY_VALUE = {"P0": 3, "P1": 2, "P2": 1, "P3": 0}
VALUE_PRIORITY = {value: key for key, value in PRIORITY_VALUE.items()}
PRESSURE_WEIGHT = {"P0": 8, "P1": 4, "P2": 2, "P3": 1}
SAFE_PARTICIPANT_FIELDS = frozenset(
    {"title", "lane", "priority", "description", "next_action", "resource", "candidate_ref", "tags"}
)


def _warning(code: str, message: str, *, task_id=None, severity="warning", **context):
    item = {"code": code, "severity": severity, "message": message}
    if task_id:
        item["task_id"] = task_id
    if context:
        item["context"] = context
    return item


class TaskCoordinator:
    """Durable task workflow with hard ownership/dependency gates and explicit evidence."""

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
        return await live_task_claims(
            self.store,
            self.agent_store,
            namespace,
            task_id,
            idle_ttl_seconds=self.session_ttl_seconds,
            max_session_seconds=self.max_session_seconds,
        )

    async def _dependencies(self, namespace, task_id):
        result = []
        for item in await self.store.dependencies(namespace, task_id):
            dep = await self.store.get_task(item["namespace"], item["task_id"])
            result.append(
                {
                    "namespace": item["namespace"],
                    "task_id": item["task_id"],
                    "state": dep["state"] if dep else "missing",
                    "archived": bool(dep and dep.get("archived_at")),
                    "satisfied": bool(dep and dep["state"] == "done"),
                }
            )
        return result

    @staticmethod
    def _valid_result(result):
        if isinstance(result, str):
            return bool(result.strip())
        if isinstance(result, (dict, list)):
            return bool(result)
        return False

    async def _open_dependencies(self, namespace, task_id):
        return [dep for dep in await self._dependencies(namespace, task_id) if not dep["satisfied"]]

    @staticmethod
    def _normalize_tags(tags):
        if tags is None:
            return None
        if not isinstance(tags, (list, tuple)):
            raise ValueError("task.tags: expected a list of strings")
        normalized = []
        for index, item in enumerate(tags):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"task.tags[{index}]: expected non-empty string")
            tag = item.strip()
            if len(tag) > 64:
                raise ValueError(f"task.tags[{index}]: maximum length is 64")
            if tag not in normalized:
                normalized.append(tag)
        if len(normalized) > 50:
            raise ValueError("task.tags: maximum 50 tags")
        return normalized

    async def _claimability(self, task, *, claims=None, dependencies=None):
        claims = (
            claims
            if claims is not None
            else await self._live_claims(task["namespace"], task["task_id"])
        )
        if dependencies is None:
            blocking = await self._open_dependencies(task["namespace"], task["task_id"])
        else:
            blocking = dependencies
        if task.get("archived_at") is not None:
            return False, claims, blocking
        if task["state"] != "ready":
            return False, claims, blocking
        if blocking:
            return False, claims, blocking
        if claims and not task.get("cooperative"):
            return False, claims, blocking
        return True, claims, blocking

    async def _cleanup_stale_claims(self, namespace, task_id):
        active = await self.store.active_claims(namespace, task_id)
        live = await self._live_claims(namespace, task_id)
        live_ids = {item["id"] for item in live}
        now = utc_text()
        for item in active:
            if item["id"] in live_ids:
                continue
            if await self.store.release_claim(namespace, task_id, item["agent_id"], now=now):
                await self.store.add_event(
                    namespace,
                    task_id,
                    "claim_released",
                    agent_id=item["agent_id"],
                    payload={"reason": "stale_session"},
                    now=now,
                )
        return live

    def _external_task(self, task):
        result = dict(task)
        result["priority"] = VALUE_PRIORITY.get(int(task.get("priority", 0)), "P3")
        result["resource_context"] = result.pop("resource", {})
        result["review_requirements"] = result.pop("reviews", [])
        return result

    @staticmethod
    def _operational_status(task, claims):
        if task["state"] != "ready":
            return task["state"]
        return "in_progress" if claims else "ready"

    def _claim_view(self, item, role):
        age = max(0, int((utc_now() - parse_utc(item["claimed_at"])).total_seconds()))
        return {
            "agent_name": public_agent_name(item["agent_id"]),
            "claimed_at": item["claimed_at"],
            "claim_age_seconds": age,
            "claim_intent": item.get("claim_intent") or "",
            "role": role,
        }

    async def _decorate(self, task, *, details=False):
        result = self._external_task(task)
        namespace, task_id = result["namespace"], result["task_id"]
        claims = await self._live_claims(namespace, task_id)
        views = [
            self._claim_view(item, "owner" if index == 0 else "participant")
            for index, item in enumerate(claims)
        ]
        result["claims"] = views
        result["owner"] = views[0] if views else None
        result["participants"] = views[1:]
        result["active"] = bool(claims)
        result["operational_status"] = self._operational_status(result, claims)
        if details:
            result["dependencies"] = await self._dependencies(namespace, task_id)
            result["relations"] = await self.store.relations(namespace, task_id)
            reviews = await self.store.reviews(namespace, task_id)
            for review in reviews:
                review["agent_name"] = public_agent_name(review.pop("agent_id"))
            result["reviews"] = reviews
            events = await self.store.list_events(namespace, task_id, limit=100)
            comments = []
            for event in events:
                if event.get("agent_id"):
                    event["agent_name"] = public_agent_name(event.pop("agent_id"))
                if event["event_type"] in {"comment", "review_feedback"}:
                    comments.append(dict(event))
            result["events"] = events
            result["comments"] = comments
        else:
            result.pop("description", None)
            result.pop("resource_context", None)
        return result

    async def _owner_error(self, agent_id, namespace, task_id, operation):
        claims = await self._live_claims(namespace, task_id)
        if claims and claims[0]["agent_id"] != agent_id:
            return {
                "ok": False,
                "code": "owner_required",
                "error": f"task.{operation}: live owner required",
                "warnings": [],
            }
        return None

    @staticmethod
    def _clean_reason(value, field, *, max_length=4000):
        text = (value or "").strip()
        if not text:
            return None, f"task.{field}: non-empty value required"
        if len(text) > max_length:
            return None, f"task.{field}: maximum length is {max_length}"
        return text, None

    async def _review_feedback_events(
        self,
        current,
        *,
        agent_id,
        outcome,
        result=None,
        blocker_reason=None,
        now,
    ):
        if current.get("lane") != "review" or outcome not in {"done", "blocked"}:
            return []
        events = []
        for relation in await self.store.relations(current["namespace"], current["task_id"]):
            if relation["direction"] != "outgoing" or relation["kind"] != "review_of":
                continue
            payload = {
                "review_namespace": current["namespace"],
                "review_task_id": current["task_id"],
                "outcome": outcome,
                "candidate_ref": current.get("candidate_ref"),
            }
            if outcome == "done":
                payload["result"] = result
            else:
                payload["findings"] = blocker_reason
                payload["blocker_reason"] = blocker_reason
            events.append(
                {
                    "namespace": relation["namespace"],
                    "task_id": relation["task_id"],
                    "event_type": "review_feedback",
                    "agent_id": agent_id,
                    "payload": payload,
                    "created_at": now,
                }
            )
        return events

    async def list(
        self,
        *,
        namespace=None,
        task_id=None,
        lane=None,
        state=None,
        operational_status=None,
        tags=None,
        show_details=False,
        show_done=False,
        show_archived=False,
        limit=50,
        cursor=None,
    ):
        if lane is not None and lane not in LANES:
            return {"ok": False, "error": f"tasks.lane: expected one of {', '.join(LANES)}"}
        if state is not None and state not in STATES:
            return {"ok": False, "error": f"tasks.state: expected one of {', '.join(STATES)}"}
        if operational_status is not None and operational_status not in OPERATIONAL_STATUSES:
            return {
                "ok": False,
                "error": (
                    f"tasks.operational_status: expected one of {', '.join(OPERATIONAL_STATUSES)}"
                ),
            }
        try:
            normalized_tags = self._normalize_tags(tags)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
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
        all_rows = await self.store.list_tasks(
            namespace=namespace,
            lane=lane,
            state=state,
            tags=normalized_tags,
            show_done=show_done,
            show_archived=show_archived,
            limit=None,
            offset=0,
        )
        vocabulary_rows = await self.store.list_tasks(
            namespace=namespace,
            lane=lane,
            state=state,
            tags=None,
            show_done=show_done,
            show_archived=show_archived,
            limit=None,
            offset=0,
        )
        tag_counts = Counter(tag for item in vocabulary_rows for tag in item.get("tags", []))
        compact = [await self._decorate(item, details=False) for item in all_rows]
        if operational_status is not None:
            compact = [item for item in compact if item["operational_status"] == operational_status]
        lane_counts = Counter(
            item["lane"]
            for item in compact
            if item["state"] != "done" and item.get("archived_at") is None
        )
        state_counts = Counter(item["state"] for item in compact)
        operational_status_counts = Counter(item["operational_status"] for item in compact)
        pressure = Counter()
        recommended = None
        claimable_count = 0
        oldest_claimable_ready_since = None
        missing_dependency_count = 0
        for item in compact:
            deps = await self._dependencies(item["namespace"], item["task_id"])
            missing_dependency_count += sum(dep["state"] == "missing" for dep in deps)
            blocking = [dep for dep in deps if not dep["satisfied"]]
            eligible, _, _ = await self._claimability(
                item, claims=item["claims"], dependencies=blocking
            )
            if not eligible:
                continue
            claimable_count += 1
            pressure[item["lane"]] += PRESSURE_WEIGHT[item["priority"]]
            ready_since = item.get("ready_since")
            if ready_since and (
                oldest_claimable_ready_since is None or ready_since < oldest_claimable_ready_since
            ):
                oldest_claimable_ready_since = ready_since
            if recommended is None:
                recommended = {
                    "namespace": item["namespace"],
                    "task_id": item["task_id"],
                    "lane": item["lane"],
                    "priority": item["priority"],
                    "title": item["title"],
                    "operational_status": item["operational_status"],
                    "tags": item.get("tags", []),
                    "ready_since": ready_since,
                }

        oldest_age = None
        if oldest_claimable_ready_since:
            oldest_age = max(
                0,
                int((utc_now() - parse_utc(oldest_claimable_ready_since)).total_seconds()),
            )
        page = compact[offset : offset + max(1, min(int(limit), 200))]
        if show_details:
            tasks = []
            for item in page:
                stored = await self.store.get_task(item["namespace"], item["task_id"])
                tasks.append(await self._decorate(stored, details=True))
        else:
            tasks = page
        next_cursor = offset + len(page) if offset + len(page) < len(compact) else None
        summary = {
            "visible": len(compact),
            "returned": len(tasks),
            "by_lane": dict(lane_counts),
            "by_state": dict(state_counts),
            "by_operational_status": dict(operational_status_counts),
            "pressure": dict(pressure),
            "tag_counts": dict(sorted(tag_counts.items())),
            "claimable_count": claimable_count,
            "oldest_claimable_ready_since": oldest_claimable_ready_since,
            "oldest_claimable_ready_age_seconds": oldest_age,
            "missing_dependency_count": missing_dependency_count,
        }
        return {
            "ok": True,
            "summary": summary,
            "tag_counts": dict(sorted(tag_counts.items())),
            "recommended": recommended,
            "tasks": tasks,
            "next_cursor": next_cursor,
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
        handler = getattr(self, f"_action_{action}", None) if action in ACTIONS else None
        if handler is None:
            return {
                "ok": False,
                "error": f"task.action: unsupported action {action}",
                "warnings": [],
            }
        if action != "create" and kwargs.get("isolation_hint") is not None:
            return {
                "ok": False,
                "error": "task.isolation_hint: set only when creating the task",
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
        if state == "done" and not self._valid_result(kwargs.get("result")):
            return {
                "ok": False,
                "error": "task.create: result required when creating a done task",
                "warnings": [],
            }
        error = self._validate(lane=lane, priority=priority, state=state)
        if error:
            return {"ok": False, "error": error, "warnings": []}
        try:
            tags = self._normalize_tags(kwargs.get("tags")) or []
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "warnings": []}
        isolation_hint, isolation_error = self._clean_reason(
            kwargs.get("isolation_hint"), "isolation_hint", max_length=160
        )
        if isolation_error:
            return {"ok": False, "error": isolation_error, "warnings": []}
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
                isolation_hint=isolation_hint,
                resource=kwargs.get("resource_context") or {},
                reviews=[],
                cooperative=bool(kwargs.get("cooperative")),
                checkpoint=kwargs.get("checkpoint") or {},
                candidate_ref=kwargs.get("candidate_ref"),
                result=kwargs.get("result"),
                tags=tags,
                dependencies=kwargs.get("dependencies"),
                event_agent_id=agent_id,
                event_payload={
                    "lane": lane,
                    "priority": priority,
                    "state": state,
                    "isolation_hint": isolation_hint,
                },
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
        if current.get("archived_at") is not None:
            return {
                "ok": False,
                "code": "archived_task",
                "error": "task.claim: task is archived",
                "warnings": [],
            }
        claim_intent, error = self._clean_reason(
            kwargs.get("claim_intent"), "claim_intent", max_length=160
        )
        if error:
            return {"ok": False, "error": error, "warnings": []}
        warnings = []
        live_claims = await self._cleanup_stale_claims(namespace, task_id)
        others = [item for item in live_claims if item["agent_id"] != agent_id]
        if others and not current.get("cooperative"):
            warning = _warning(
                "already_claimed",
                "Non-cooperative task already has a live owner.",
                task_id=task_id,
                claims=[public_agent_name(item["agent_id"]) for item in others],
                cooperative=False,
            )
            result = await self._result(
                namespace, task_id, [warning], ok=False, error="task.claim: already_claimed"
            )
            result["code"] = "already_claimed"
            return result
        if others:
            warnings.append(
                _warning(
                    "already_claimed",
                    "Cooperative task already has active claims; additional claim is allowed.",
                    task_id=task_id,
                    severity="info",
                    claims=[public_agent_name(item["agent_id"]) for item in others],
                    cooperative=True,
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
        open_deps = await self._open_dependencies(namespace, task_id)
        dependency_override = None
        if open_deps:
            dependency_warning = _warning(
                "dependency_open",
                "Task has unfinished dependencies.",
                task_id=task_id,
                dependencies=open_deps,
            )
            force = bool(kwargs.get("force"))
            force_reason = (kwargs.get("force_reason") or "").strip()
            if not force:
                result = await self._result(
                    namespace,
                    task_id,
                    [*warnings, dependency_warning],
                    ok=False,
                    error=(
                        "task.claim: dependency_open; use force=true with force_reason "
                        "if genuinely necessary"
                    ),
                )
                result["code"] = "dependency_open"
                result["blocking_dependencies"] = open_deps
                return result
            if not force_reason:
                result = await self._result(
                    namespace,
                    task_id,
                    [*warnings, dependency_warning],
                    ok=False,
                    error="task.claim.force_reason: required when forcing open dependencies",
                )
                result["code"] = "dependency_open"
                result["blocking_dependencies"] = open_deps
                return result
            warnings.extend(
                [
                    dependency_warning,
                    _warning(
                        "dependency_forced",
                        "Open dependencies were explicitly overridden for this claim.",
                        task_id=task_id,
                        dependencies=open_deps,
                        force_reason=force_reason,
                    ),
                ]
            )
            dependency_override = {
                "blocking_dependencies": open_deps,
                "force_reason": force_reason,
            }
        now = utc_text()
        try:
            await self.store.claim(
                namespace,
                task_id,
                agent_id,
                claim_intent=claim_intent,
                exclusive=not bool(current.get("cooperative")),
                event_payload={"warnings": [item["code"] for item in warnings]},
                dependency_override=dependency_override,
                now=now,
            )
        except TaskClaimConflict as exc:
            warning = _warning(
                "already_claimed",
                "Non-cooperative task already has a live owner.",
                task_id=task_id,
                claims=[public_agent_name(item) for item in exc.agent_ids],
                cooperative=False,
            )
            result = await self._result(
                namespace, task_id, [warning], ok=False, error="task.claim: already_claimed"
            )
            result["code"] = "already_claimed"
            return result
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": f"task.claim: {exc}", "warnings": warnings}
        self._inc("terminal_mcp_task_claims_total")
        return await self._result(namespace, task_id, warnings)

    async def _action_release(self, agent_id, namespace, task_id, **kwargs):
        if not await self._required(namespace, task_id):
            return self._missing()
        claims = await self._live_claims(namespace, task_id)
        own = next((item for item in claims if item["agent_id"] == agent_id), None)
        if not own:
            return await self._result(
                namespace,
                task_id,
                [
                    _warning(
                        "not_claimed",
                        "Agent has no live claim on task.",
                        task_id=task_id,
                        severity="info",
                    )
                ],
            )
        reason, error = self._clean_reason(kwargs.get("release_reason"), "release_reason")
        if error:
            return {"ok": False, "error": error, "warnings": []}
        await self.store.release_claim_mutation(
            namespace, task_id, agent_id, reason=reason, now=utc_text()
        )
        return await self._result(namespace, task_id, [])

    async def _action_checkpoint(self, agent_id, namespace, task_id, **kwargs):
        if not await self._required(namespace, task_id):
            return self._missing()
        owner_error = await self._owner_error(agent_id, namespace, task_id, "checkpoint")
        if owner_error:
            return owner_error
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

    async def _action_comment(self, agent_id, namespace, task_id, **kwargs):
        if not await self._required(namespace, task_id):
            return self._missing()
        text, error = self._clean_reason(kwargs.get("comment_text"), "comment_text")
        if error:
            return {"ok": False, "error": error, "warnings": []}
        await self.store.add_event(
            namespace,
            task_id,
            "comment",
            agent_id=agent_id,
            payload={"text": text, "kind": "comment"},
            now=utc_text(),
        )
        return await self._result(namespace, task_id, [])

    async def _action_relate(self, agent_id, namespace, task_id, **kwargs):
        if not await self._required(namespace, task_id):
            return self._missing()
        owner_error = await self._owner_error(agent_id, namespace, task_id, "relate")
        if owner_error:
            return owner_error
        kind = (kwargs.get("relation_kind") or "").strip()
        related_namespace = (kwargs.get("related_namespace") or namespace).strip()
        related_task_id = (kwargs.get("related_task_id") or "").strip()
        if not kind or not related_task_id:
            return {
                "ok": False,
                "error": "task.relate: relation_kind and related_task_id required",
                "warnings": [],
            }
        if len(kind) > 64:
            return {
                "ok": False,
                "error": "task.relation_kind: maximum length is 64",
                "warnings": [],
            }
        try:
            await self.store.add_relation(
                namespace,
                task_id,
                related_namespace=related_namespace,
                related_task_id=related_task_id,
                relation_kind=kind,
                agent_id=agent_id,
            )
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": f"task.relate: {exc}", "warnings": []}
        return await self._result(namespace, task_id, [])

    async def _action_unrelate(self, agent_id, namespace, task_id, **kwargs):
        if not await self._required(namespace, task_id):
            return self._missing()
        owner_error = await self._owner_error(agent_id, namespace, task_id, "unrelate")
        if owner_error:
            return owner_error
        kind = (kwargs.get("relation_kind") or "").strip()
        related_namespace = (kwargs.get("related_namespace") or namespace).strip()
        related_task_id = (kwargs.get("related_task_id") or "").strip()
        if not kind or not related_task_id:
            return {
                "ok": False,
                "error": "task.unrelate: relation_kind and related_task_id required",
                "warnings": [],
            }
        await self.store.remove_relation(
            namespace,
            task_id,
            related_namespace=related_namespace,
            related_task_id=related_task_id,
            relation_kind=kind,
            agent_id=agent_id,
        )
        return await self._result(namespace, task_id, [])

    async def _action_update(self, agent_id, namespace, task_id, **kwargs):
        if not await self._required(namespace, task_id):
            return self._missing()
        return await self._update_from_kwargs(agent_id, namespace, task_id, kwargs)

    async def _action_state(self, agent_id, namespace, task_id, **kwargs):
        if kwargs.get("state") is None:
            return {"ok": False, "error": "task.state: state required", "warnings": []}
        return await self._action_update(agent_id, namespace, task_id, **kwargs)

    async def _action_done(self, agent_id, namespace, task_id, **kwargs):
        current = await self._required(namespace, task_id)
        if not current:
            return self._missing()
        if current["state"] != "done" and not self._valid_result(kwargs.get("result")):
            return {"ok": False, "error": "task.done: result required", "warnings": []}
        kwargs["state"] = "done"
        return await self._action_update(agent_id, namespace, task_id, **kwargs)

    async def _action_archive(self, agent_id, namespace, task_id, **kwargs):
        current = await self._required(namespace, task_id)
        if not current:
            return self._missing()
        owner_error = await self._owner_error(agent_id, namespace, task_id, "archive")
        if owner_error:
            return owner_error
        note, error = self._clean_reason(
            kwargs.get("archive_note") or kwargs.get("note"), "archive_note"
        )
        if error:
            return {"ok": False, "error": error, "warnings": []}
        if current.get("archived_at") is not None:
            return await self._result(
                namespace,
                task_id,
                [
                    _warning(
                        "already_archived",
                        "Task is already archived.",
                        task_id=task_id,
                        severity="info",
                    )
                ],
            )
        now = utc_text()
        return await self._update(
            agent_id,
            namespace,
            task_id,
            {"archived_at": now, "archive_note": note},
            kwargs.get("expected_revision"),
            "archived",
            release_claims_reason="task_archived",
            event_extra={"archive_note": note, "state": current["state"]},
        )

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
            "cooperative": "cooperative",
            "checkpoint": "checkpoint",
            "candidate_ref": "candidate_ref",
            "result": "result",
            "tags": "tags",
        }
        for external, internal in mapping.items():
            if kwargs.get(external) is not None:
                fields[internal] = kwargs[external]
        if kwargs.get("tags") is not None:
            try:
                fields["tags"] = self._normalize_tags(kwargs["tags"])
            except ValueError as exc:
                return {"ok": False, "error": str(exc), "warnings": []}
        if kwargs.get("priority") is not None:
            fields["priority"] = PRIORITY_VALUE.get(kwargs["priority"], -1)
        error = self._validate(
            lane=kwargs.get("lane"),
            priority=kwargs.get("priority"),
            state=kwargs.get("state"),
        )
        if error:
            return {"ok": False, "error": error, "warnings": []}
        warnings = []
        target_state = kwargs.get("state")
        claims = await self._live_claims(namespace, task_id)
        owner = claims[0]["agent_id"] if claims else None
        unsafe_fields = set(fields) - SAFE_PARTICIPANT_FIELDS
        workflow_change = bool(unsafe_fields or kwargs.get("dependencies") is not None)
        if owner is not None and owner != agent_id and workflow_change:
            return {
                "ok": False,
                "code": "owner_required",
                "error": "task.update: live owner required for workflow-changing mutation",
                "warnings": [],
            }
        if "cooperative" in fields and not bool(fields["cooperative"]) and len(claims) > 1:
            return {
                "ok": False,
                "error": (
                    "task.update: cooperative=false requires releasing extra live claims first"
                ),
                "warnings": [],
            }
        blocker_reason = None
        if target_state == "blocked" and current["state"] != "blocked" and claims:
            blocker_reason, blocker_error = self._clean_reason(
                kwargs.get("blocker_reason"), "blocker_reason"
            )
            if blocker_error:
                return {"ok": False, "error": blocker_error, "warnings": []}
        if target_state == "archived":
            return {
                "ok": False,
                "error": "task.update: use action=archive with note",
                "warnings": [],
            }
        if (
            target_state == "done"
            and current["state"] != "done"
            and not self._valid_result(kwargs.get("result"))
        ):
            return {"ok": False, "error": "task.done: result required", "warnings": []}
        if current["state"] in {"blocked", "archived"} and target_state not in {
            None,
            current["state"],
        }:
            warnings.append(
                _warning(
                    "unusual_transition",
                    f"{current['state'].capitalize()} task state is being changed explicitly.",
                    task_id=task_id,
                    from_state=current["state"],
                    to_state=target_state,
                )
            )
        event_extra = {}
        if blocker_reason is not None:
            event_extra["blocker_reason"] = blocker_reason
        additional_events = []
        if target_state in {"done", "blocked"}:
            additional_events = await self._review_feedback_events(
                current,
                agent_id=agent_id,
                outcome=target_state,
                result=kwargs.get("result"),
                blocker_reason=blocker_reason,
                now=utc_text(),
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
            release_claims_reason="task_done" if target_state == "done" else None,
            event_extra=event_extra or None,
            additional_events=additional_events,
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
        release_claims_reason=None,
        event_extra=None,
        additional_events=None,
    ):
        warnings = list(warnings or [])
        now = utc_text()
        event_payload = {
            "fields": sorted(fields),
            "warnings": [item["code"] for item in warnings],
        }
        if event_extra:
            event_payload.update(event_extra)
        for key in (
            "checkpoint",
            "candidate_ref",
            "result",
            "tags",
            "state",
            "lane",
            "priority",
            "next_action",
        ):
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
                release_claims_reason=release_claims_reason,
                additional_events=additional_events,
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
            if not session_is_live(
                session,
                now=now,
                idle_ttl_seconds=self.session_ttl_seconds,
                max_session_seconds=self.max_session_seconds,
            ):
                stale_claims += 1
        unreleased_claims = stats["active_claims"]
        live_claims = max(0, unreleased_claims - stale_claims)
        stats["unreleased_claims"] = unreleased_claims
        stats["live_claims"] = live_claims
        stats["stale_claims"] = stale_claims
        stats["ok"] = True
        if self.metrics:
            for state, count in stats["by_state"].items():
                self.metrics.set("terminal_mcp_tasks", count, (("state", state),))
            for lane, count in stats["by_lane"].items():
                self.metrics.set("terminal_mcp_tasks_by_lane", count, (("lane", lane),))
            # Compatibility metric: active_claims historically meant unreleased persisted rows.
            self.metrics.set("terminal_mcp_task_active_claims", unreleased_claims)
            self.metrics.set("terminal_mcp_task_unreleased_claims", unreleased_claims)
            self.metrics.set("terminal_mcp_task_live_claims", live_claims)
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
        result = []
        for item in rows:
            claims = await self._live_claims(item["namespace"], item["task_id"])
            if not any(claim["agent_id"] == agent_id for claim in claims):
                continue
            result.append(
                {
                    "namespace": item["namespace"],
                    "task_id": item["task_id"],
                    "lane": item["lane"],
                    "priority": VALUE_PRIORITY.get(int(item["priority"]), "P3"),
                    "state": item["state"],
                    "operational_status": item["state"]
                    if item["state"] != "ready"
                    else "in_progress",
                    "isolation_hint": item["isolation_hint"],
                }
            )
        return result

    async def record_command(self, agent_id, command_hash, command_type, task_refs):
        now = utc_text()
        for item in task_refs:
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
