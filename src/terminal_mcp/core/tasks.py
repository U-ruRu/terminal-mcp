from __future__ import annotations

import json
import logging
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
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.core.public_errors import ValidationIssue, ValidationRepair, public_error
from terminal_mcp.storage.tasks import (
    TaskAgentBusy,
    TaskClaimConflict,
    TaskCommittedRecord,
    TaskOwnershipConflict,
    TaskRelationConflict,
    TaskRevisionConflict,
)

LANES = ("implementation", "review", "release", "integration", "general")
STATES = ("ready", "in_progress", "blocked", "deferred", "done")
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
    "review",
)
REVIEW_DIMENSIONS = ("A", "C", "R")  # legacy read-only review history
PRIORITY_VALUE = {"P0": 3, "P1": 2, "P2": 1, "P3": 0}
VALUE_PRIORITY = {value: key for key, value in PRIORITY_VALUE.items()}
PRESSURE_WEIGHT = {"P0": 8, "P1": 4, "P2": 2, "P3": 1}
SAFE_PARTICIPANT_FIELDS = frozenset({"title", "description", "next_action", "tags"})
DESCRIPTION_PREVIEW_LIMIT = 1500
CHECKPOINT_TEXT_LIMIT = 4000


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
            try:
                self.metrics.inc(name, labels)
            except Exception:
                logging.getLogger(__name__).warning("Task metric recording failed", exc_info=True)

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

    async def _dependency_views_from_input(self, namespace, task_id, dependencies):
        result = []
        for item in dependencies or []:
            dep_namespace = item.get("namespace") or namespace
            dep_task_id = item["task_id"]
            if dep_namespace == namespace and dep_task_id == task_id:
                continue
            dep = await self.store.get_task(dep_namespace, dep_task_id)
            result.append(
                {
                    "namespace": dep_namespace,
                    "task_id": dep_task_id,
                    "state": dep["state"] if dep else "missing",
                    "archived": bool(dep and dep.get("archived_at")),
                    "satisfied": bool(dep and dep["state"] == "done"),
                }
            )
        return result

    async def _dependency_gate(
        self,
        namespace,
        task_id,
        *,
        operation,
        force=False,
        force_reason=None,
        warnings=None,
        blocking_dependencies=None,
        task_exists=True,
    ):
        warnings = list(warnings or [])
        open_deps = (
            list(blocking_dependencies)
            if blocking_dependencies is not None
            else await self._open_dependencies(namespace, task_id)
        )
        if not open_deps:
            return warnings, None, None

        dependency_warning = _warning(
            "dependency_open",
            "Task has unfinished dependencies.",
            task_id=task_id,
            dependencies=open_deps,
        )
        blocked_warnings = [*warnings, dependency_warning]
        if not force:
            error = (
                f"task.{operation}: dependency_open; use force=true with force_reason "
                "if genuinely necessary"
            )
            result = (
                await self._result(
                    namespace,
                    task_id,
                    blocked_warnings,
                    ok=False,
                    error=error,
                )
                if task_exists
                else {"ok": False, "warnings": blocked_warnings, "error": error}
            )
            result["code"] = "dependency_open"
            result["blocking_dependencies"] = open_deps
            return warnings, None, result

        normalized_reason = (force_reason or "").strip()
        if not normalized_reason:
            error = f"task.{operation}.force_reason: required when forcing open dependencies"
            result = (
                await self._result(
                    namespace,
                    task_id,
                    blocked_warnings,
                    ok=False,
                    error=error,
                )
                if task_exists
                else {"ok": False, "warnings": blocked_warnings, "error": error}
            )
            result["code"] = "dependency_open"
            result["blocking_dependencies"] = open_deps
            return warnings, None, result

        warnings.extend(
            [
                dependency_warning,
                _warning(
                    "dependency_forced",
                    f"Open dependencies were explicitly overridden for this {operation}.",
                    task_id=task_id,
                    dependencies=open_deps,
                    force_reason=normalized_reason,
                ),
            ]
        )
        return (
            warnings,
            {
                "blocking_dependencies": open_deps,
                "force_reason": normalized_reason,
                "operation": operation,
            },
            None,
        )

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

    @staticmethod
    def _normalize_refs(refs, field):
        if refs is None:
            return []
        if not isinstance(refs, list):
            raise ValueError(f"task.{field}: expected list")
        if len(refs) > 64:
            raise ValueError(f"task.{field}: maximum 64 refs")
        result = []
        seen = set()
        for index, value in enumerate(refs):
            if not isinstance(value, str):
                raise ValueError(f"task.{field}[{index}]: expected string")
            if not value:
                raise ValueError(f"task.{field}[{index}]: empty ref is not allowed")
            if value != value.strip():
                raise ValueError(
                    f"task.{field}[{index}]: leading/trailing whitespace is not allowed"
                )
            if len(value) > 512:
                raise ValueError(f"task.{field}[{index}]: maximum length is 512 characters")
            if value not in seen:
                seen.add(value)
                result.append(value)
        return result

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
    def _operational_status(task, claims, blocking_dependencies=None):
        # Execution state is explicit and independent from claim ownership.
        if task["state"] != "ready":
            return task["state"]
        return "blocked" if blocking_dependencies else "ready"

    def _claim_view(self, item, role, *, reveal_agent_id=False):
        age = max(0, int((utc_now() - parse_utc(item["claimed_at"])).total_seconds()))
        result = {
            "agent_name": public_agent_name(item["agent_id"]),
            "claimed_at": item["claimed_at"],
            "claim_age_seconds": age,
            "claim_intent": item.get("claim_intent") or "",
            "role": role,
        }
        if reveal_agent_id:
            result["agent_id"] = item["agent_id"]
        return result

    @staticmethod
    def _checkpoint_text(value):
        if value in (None, "", {}, []):
            return None
        if isinstance(value, str):
            text = value
        elif isinstance(value, dict) and isinstance(value.get("text"), str):
            text = value["text"]
        else:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return text[:CHECKPOINT_TEXT_LIMIT]

    async def _latest_checkpoint(self, namespace, task_id, task):
        event = await self.store.latest_checkpoint_event(namespace, task_id)
        if event is not None:
            payload = event.get("payload") or {}
            text = self._checkpoint_text(payload.get("checkpoint"))
            if text is not None:
                agent_id = event.get("agent_id")
                return {
                    "text": text,
                    "author": public_agent_name(agent_id) if agent_id else "system",
                    "created_at": event["created_at"],
                    "revision": int(payload.get("revision") or task.get("revision") or 1),
                }
        text = self._checkpoint_text(task.get("checkpoint"))
        if text is None:
            return None
        return {
            "text": text,
            "author": "unknown",
            "created_at": task.get("updated_at") or task.get("created_at") or utc_text(),
            "revision": int(task.get("revision") or 1),
        }

    async def _snapshot(self, task, *, reveal_agent_ids=False):
        external = self._external_task(task)
        namespace, task_id = external["namespace"], external["task_id"]
        claims = await self._live_claims(namespace, task_id)
        claim = (
            self._claim_view(claims[0], "owner", reveal_agent_id=reveal_agent_ids)
            if claims
            else None
        )
        dependencies = await self._dependencies(namespace, task_id)
        blocking_dependencies = [dep for dep in dependencies if not dep["satisfied"]]
        description = external.get("description") or ""
        return {
            "namespace": namespace,
            "task_id": task_id,
            "title": external["title"],
            "lane": external["lane"],
            "priority": external["priority"],
            "state": external["state"],
            "operational_status": self._operational_status(external, claims, blocking_dependencies),
            "revision": external["revision"],
            "claim": claim,
            "next_action": external.get("next_action") or "",
            "description_preview": description[:DESCRIPTION_PREVIEW_LIMIT],
            "description_truncated": len(description) > DESCRIPTION_PREVIEW_LIMIT,
            "latest_checkpoint": await self._latest_checkpoint(namespace, task_id, external),
            "blocking_dependencies": blocking_dependencies,
        }

    def _decorate_prefetched(
        self,
        task,
        claims,
        dependencies,
        sessions_by_agent,
        *,
        now=None,
        reveal_agent_ids=False,
    ):
        result = self._external_task(task)
        current = now or utc_now()
        if self.agent_store is not None:
            claims = [
                claim
                for claim in claims
                if claim.get("owner_kind", "legacy_session") == "logical_agent"
                or session_is_live(
                    sessions_by_agent.get(claim.get("owner_id") or claim["agent_id"]),
                    now=current,
                    idle_ttl_seconds=self.session_ttl_seconds,
                    max_session_seconds=self.max_session_seconds,
                )
            ]
        views = [
            self._claim_view(
                item,
                "owner" if index == 0 else "participant",
                reveal_agent_id=reveal_agent_ids,
            )
            for index, item in enumerate(claims)
        ]
        blocking_dependencies = [dep for dep in dependencies if not dep["satisfied"]]
        result["claims"] = views
        result["owner"] = views[0] if views else None
        result["participants"] = views[1:]
        result["active"] = bool(claims)
        result["blocking_dependencies"] = blocking_dependencies
        result["operational_status"] = self._operational_status(
            result, claims, blocking_dependencies
        )
        result.pop("description", None)
        result.pop("resource_context", None)
        return result

    async def _decorate(self, task, *, details=False, reveal_agent_ids=False):
        result = self._external_task(task)
        namespace, task_id = result["namespace"], result["task_id"]
        claims = await self._live_claims(namespace, task_id)
        views = [
            self._claim_view(
                item,
                "owner" if index == 0 else "participant",
                reveal_agent_id=reveal_agent_ids,
            )
            for index, item in enumerate(claims)
        ]
        dependencies = await self._dependencies(namespace, task_id)
        blocking_dependencies = [dep for dep in dependencies if not dep["satisfied"]]
        result["claims"] = views
        result["owner"] = views[0] if views else None
        result["participants"] = views[1:]
        result["active"] = bool(claims)
        result["blocking_dependencies"] = blocking_dependencies
        result["operational_status"] = self._operational_status(
            result, claims, blocking_dependencies
        )
        if details:
            result["dependencies"] = dependencies
            result["relations"] = await self.store.relations(namespace, task_id)
            reviews = await self.store.reviews(namespace, task_id)
            for review in reviews:
                reviewer = public_agent_name(review.pop("agent_id"))
                review["reviewer"] = reviewer
                review["agent_name"] = reviewer
                review.pop("candidate_ref", None)
            result["reviews"] = reviews
            result["output_states"] = await self.store.output_states(namespace, task_id)
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

    async def _claim_snapshot(self, namespace, task_id):
        return tuple(claim["id"] for claim in await self.store.active_claims(namespace, task_id))

    @staticmethod
    def _effective_revision(kwargs, current):
        value = kwargs.get("expected_revision")
        return current["revision"] if value is None else value

    @staticmethod
    def _ownership_conflict_result():
        return {
            "ok": False,
            "code": "owner_required",
            "error": "Task ownership changed before commit; refresh ownership and retry.",
            "warnings": [],
            "outcome": "not_committed",
        }

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
    def _text_input_failure(field, *, max_length=4000, missing=False):
        return public_error(
            "input_validation_failed",
            reason="missing_required" if missing else "constraint_violation",
            path=field,
            details=ValidationRepair(
                validation_errors=(
                    ValidationIssue(
                        error_class="missing" if missing else "invalid_value",
                        path=field,
                        description=(
                            f"Provide non-whitespace text of at most {max_length} characters."
                        ),
                    ),
                )
            ),
        ).as_dict()

    @staticmethod
    def _clean_reason(value, field, *, max_length=4000):
        text = (value or "").strip()
        if not text:
            return None, f"task.{field}: non-empty value required"
        if len(text) > max_length:
            return None, f"task.{field}: maximum length is {max_length}"
        return text, None

    async def _review_completion_candidate_error(self, current, proposed_candidate=None):
        if current.get("lane") != "review":
            return None
        review_relations = [
            relation
            for relation in await self.store.relations(current["namespace"], current["task_id"])
            if relation["direction"] == "outgoing" and relation["kind"] == "review_of"
        ]
        if not review_relations:
            return None
        review_candidate = (
            proposed_candidate if proposed_candidate is not None else current.get("candidate_ref")
        )
        review_candidate = (review_candidate or "").strip()
        if len(review_relations) != 1:
            parent_candidate = None
        else:
            relation = review_relations[0]
            parent = await self.store.get_task(relation["namespace"], relation["task_id"])
            parent_candidate = ((parent or {}).get("candidate_ref") or "").strip()
        if not review_candidate or not parent_candidate or review_candidate != parent_candidate:
            return {
                "ok": False,
                "code": "candidate_mismatch",
                "error": "task.done: review candidate_ref does not match review_of parent",
                "warnings": [],
            }
        return None

    async def _review_requirements_error(self, current, proposed_output_refs=None):
        requirements = current.get("reviews") or []
        if not requirements:
            return None
        if proposed_output_refs is not None and proposed_output_refs != current.get(
            "output_refs", []
        ):
            missing = list(requirements)
        else:
            rows = await self.store.reviews(
                current["namespace"],
                current["task_id"],
                output_state_id=current.get("output_state_id"),
            )
            approved = {row["dimension"] for row in rows if row["verdict"] == "NON_BLOCKING"}
            missing = [item for item in requirements if item not in approved]
        if not missing:
            return None
        return {
            "ok": False,
            "code": "review_requirements_unsatisfied",
            "error": (
                "task.done: current output state is missing NON_BLOCKING reviews for "
                + ",".join(missing)
            ),
            "warnings": [],
        }

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
        snapshot=False,
        show_done=False,
        show_archived=False,
        limit=50,
        cursor=None,
        reveal_agent_ids=False,
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
                "task": (
                    await self._snapshot(task, reveal_agent_ids=reveal_agent_ids)
                    if task and snapshot
                    else await self._decorate(
                        task, details=show_details, reveal_agent_ids=reveal_agent_ids
                    )
                    if task
                    else None
                ),
                "error": None if task else "task not found",
                **({"code": "task_not_found"} if task is None else {}),
            }

        raw_cursor = max(0, int(cursor or 0))
        page_limit = max(1, min(int(limit), 1000))
        required_tags = set(normalized_tags or [])
        runtime_state = await self.store.runtime_state_snapshot()
        sessions_by_agent = {}
        if self.agent_store is not None:
            sessions_by_agent = {
                session["agent_id"]: session for session in await self.agent_store.active_sessions()
            }
        now = utc_now()
        lane_counts = Counter()
        state_counts = Counter()
        operational_status_counts = Counter()
        pressure = Counter()
        tag_counts = Counter()
        recommended = None
        claimable_count = 0
        oldest_claimable_ready_since = None
        missing_dependency_count = 0
        visible_count = 0
        page_matches: list[tuple[int, dict]] = []
        scan_offset = 0
        scan_limit = max(50, min(200, page_limit * 2))

        while True:
            batch = await self.store.list_tasks(
                namespace=namespace,
                lane=lane,
                state=state,
                tags=None,
                show_done=show_done,
                show_archived=show_archived,
                limit=scan_limit,
                offset=scan_offset,
            )
            if not batch:
                break
            for index, row in enumerate(batch):
                raw_position = scan_offset + index + 1
                tag_counts.update(row.get("tags", []))
                if required_tags and not required_tags.issubset(set(row.get("tags", []))):
                    continue
                item = self._decorate_prefetched(
                    row,
                    runtime_state["claims"].get((row["namespace"], row["task_id"]), []),
                    runtime_state["dependencies"].get((row["namespace"], row["task_id"]), []),
                    sessions_by_agent,
                    now=now,
                    reveal_agent_ids=reveal_agent_ids,
                )
                if (
                    operational_status is not None
                    and item["operational_status"] != operational_status
                ):
                    continue
                visible_count += 1
                if item["state"] != "done" and item.get("archived_at") is None:
                    lane_counts[item["lane"]] += 1
                state_counts[item["state"]] += 1
                operational_status_counts[item["operational_status"]] += 1
                blocking = item["blocking_dependencies"]
                missing_dependency_count += sum(dep["state"] == "missing" for dep in blocking)
                eligible, _, _ = await self._claimability(
                    item, claims=item["claims"], dependencies=blocking
                )
                if eligible:
                    claimable_count += 1
                    pressure[item["lane"]] += PRESSURE_WEIGHT[item["priority"]]
                    ready_since = item.get("ready_since")
                    if ready_since and (
                        oldest_claimable_ready_since is None
                        or ready_since < oldest_claimable_ready_since
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
                if raw_position > raw_cursor and len(page_matches) < page_limit + 1:
                    page_matches.append((raw_position, item))
            scan_offset += len(batch)
            if len(batch) < scan_limit:
                break

        selected = page_matches[:page_limit]
        if show_details:
            tasks = []
            for _position, item in selected:
                stored = await self.store.get_task(item["namespace"], item["task_id"])
                tasks.append(
                    await self._decorate(
                        stored,
                        details=True,
                        reveal_agent_ids=reveal_agent_ids,
                    )
                )
        else:
            tasks = [item for _position, item in selected]

        has_more = len(page_matches) > page_limit
        next_cursor = selected[-1][0] if has_more and selected else None
        oldest_age = None
        if oldest_claimable_ready_since:
            oldest_age = max(
                0,
                int((utc_now() - parse_utc(oldest_claimable_ready_since)).total_seconds()),
            )
        summary = {
            "visible": visible_count,
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
            "_cursor_positions": [position for position, _item in selected],
        }

    async def mutate(
        self, agent_id, *, action, namespace, task_id=None, _claim_owner=None, **kwargs
    ):
        if not namespace:
            return {"ok": False, "error": "task.namespace: required", "warnings": []}
        if self.agent_store and _claim_owner is None:
            session = await self.agent_store.get_session(agent_id)
            if not session or session["state"] != "active":
                return {
                    "ok": False,
                    "error": "task.agent_id: active session required",
                    "warnings": [],
                }
        if _claim_owner is not None:
            kwargs["_claim_owner"] = _claim_owner
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
        try:
            # Reject stale no-op calls before action-specific early returns.
            # Storage repeats this check inside each write transaction.
            expected = kwargs.get("expected_revision")
            if expected is not None and action in {
                "claim",
                "release",
                "review",
                "relate",
                "unrelate",
            }:
                current = await self.store.get_task(namespace, task_id)
                if current is not None and int(current["revision"]) != int(expected):
                    raise TaskRevisionConflict(
                        namespace, task_id, int(expected), current["revision"]
                    )
            return await handler(agent_id, namespace, task_id, **kwargs)
        except TaskOwnershipConflict:
            return self._ownership_conflict_result()
        except TaskRevisionConflict as exc:
            return await self._result(
                namespace,
                task_id,
                [],
                ok=False,
                error=str(exc),
                code="revision_conflict",
                details={"current_revision": exc.actual},
            )

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
        try:
            input_refs = self._normalize_refs(kwargs.get("input_refs") or [], "input_refs")
            if "output_refs" in kwargs:
                output_refs = self._normalize_refs(kwargs.get("output_refs") or [], "output_refs")
            elif kwargs.get("candidate_ref"):
                output_refs = self._normalize_refs([kwargs["candidate_ref"]], "output_refs")
            else:
                output_refs = []
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "warnings": []}
        isolation_hint, isolation_error = self._clean_reason(
            kwargs.get("isolation_hint"), "isolation_hint", max_length=160
        )
        if isolation_error:
            return self._text_input_failure(
                "isolation_hint",
                max_length=160,
                missing=kwargs.get("isolation_hint") is None,
            )

        warnings = []
        dependency_override = None
        if state == "done":
            proposed_dependencies = await self._dependency_views_from_input(
                namespace, task_id, kwargs.get("dependencies")
            )
            blocking_dependencies = [dep for dep in proposed_dependencies if not dep["satisfied"]]
            warnings, dependency_override, dependency_failure = await self._dependency_gate(
                namespace,
                task_id,
                operation="done",
                force=bool(kwargs.get("force")),
                force_reason=kwargs.get("force_reason"),
                warnings=warnings,
                blocking_dependencies=blocking_dependencies,
                task_exists=False,
            )
            if dependency_failure:
                return dependency_failure

        now = utc_text()
        try:
            committed = await self.store.create_task_mutation(
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
                input_refs=input_refs,
                output_refs=output_refs,
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
                dependency_override=dependency_override,
                now=now,
            )
        except Exception as exc:
            return {"ok": False, "error": f"task.create: {exc}", "warnings": []}
        self._inc("terminal_mcp_tasks_created_total")
        return await self._result(
            namespace,
            task_id,
            warnings,
            committed_task=committed,
            include_description=kwargs.get("description") is not None,
        )

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
            return self._text_input_failure(
                "claim_intent",
                max_length=160,
                missing=kwargs.get("claim_intent") is None,
            )
        warnings = []
        live_claims = await self._cleanup_stale_claims(namespace, task_id)
        # Stale-lease cleanup may advance the revision. The admission policy and
        # optimistic write guard must refer to the same refreshed record.
        current = await self._required(namespace, task_id)
        if not current:
            return self._missing()
        if current.get("archived_at") is not None:
            return {
                "ok": False,
                "code": "archived_task",
                "error": "Task is archived.",
                "warnings": [],
            }
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
        warnings, dependency_override, dependency_failure = await self._dependency_gate(
            namespace,
            task_id,
            operation="claim",
            force=bool(kwargs.get("force")),
            force_reason=kwargs.get("force_reason"),
            warnings=warnings,
        )
        if dependency_failure:
            return dependency_failure
        now = utc_text()
        try:
            owner = kwargs.get("_claim_owner")
            claim_method = self.store.claim_owner if owner is not None else self.store.claim
            claim_identity = owner if owner is not None else agent_id
            lease_kwargs = {}
            if owner is not None and kwargs.get("_claim_lease") is not None:
                lease_kwargs["lease"] = kwargs["_claim_lease"]
            committed = await claim_method(
                namespace,
                task_id,
                claim_identity,
                claim_intent=claim_intent,
                exclusive=not bool(current.get("cooperative")),
                event_payload={"warnings": [item["code"] for item in warnings]},
                dependency_override=dependency_override,
                now=now,
                expected_revision=self._effective_revision(kwargs, current),
                capture_task=True,
                **lease_kwargs,
            )
        except TaskAgentBusy as exc:
            warning = _warning(
                "agent_busy",
                "Slot already owns another live managed-task claim.",
                task_id=task_id,
                current_namespace=exc.namespace,
                current_task_id=exc.task_id,
                current_claimed_at=exc.claimed_at,
            )
            result = await self._result(
                namespace,
                task_id,
                [warning],
                ok=False,
                error=f"task.claim: agent_busy on {exc.namespace}/{exc.task_id}",
            )
            result["code"] = "agent_busy"
            return result
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
        return await self._result(namespace, task_id, warnings, committed_task=committed)

    async def _action_release(self, agent_id, namespace, task_id, **kwargs):
        current = await self._required(namespace, task_id)
        if not current:
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
            return self._text_input_failure(
                "release_reason",
                max_length=4000,
                missing=kwargs.get("release_reason") is None,
            )
        owner = kwargs.get("_claim_owner")
        if owner is None:
            committed = await self.store.release_claim_mutation(
                namespace,
                task_id,
                agent_id,
                reason=reason,
                now=utc_text(),
                expected_revision=self._effective_revision(kwargs, current),
                expected_claim_id=own["id"],
                capture_task=True,
            )
        else:
            committed = await self.store.release_owner_claim_mutation(
                namespace,
                task_id,
                owner,
                reason=reason,
                now=utc_text(),
                expected_revision=self._effective_revision(kwargs, current),
                expected_claim_id=own["id"],
                capture_task=True,
            )
        return await self._result(namespace, task_id, [], committed_task=committed)

    async def _action_checkpoint(self, agent_id, namespace, task_id, **kwargs):
        current = await self._required(namespace, task_id)
        if not current:
            return self._missing()
        ownership = await self._claim_snapshot(namespace, task_id)
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
            self._effective_revision(kwargs, current),
            "checkpoint",
            expected_claim_ids=ownership,
        )

    async def _action_comment(self, agent_id, namespace, task_id, **kwargs):
        current = await self._required(namespace, task_id)
        if not current:
            return self._missing()
        text, error = self._clean_reason(kwargs.get("comment_text"), "comment_text")
        if error:
            return self._text_input_failure(
                "comment_text",
                max_length=4000,
                missing=kwargs.get("comment_text") is None,
            )
        committed = await self.store.add_event(
            namespace,
            task_id,
            "comment",
            agent_id=agent_id,
            payload={"text": text, "kind": "comment"},
            now=utc_text(),
            capture_task=True,
        )
        return await self._result(namespace, task_id, [], committed_task=committed)

    async def _action_relate(self, agent_id, namespace, task_id, **kwargs):
        current = await self._required(namespace, task_id)
        if not current:
            return self._missing()
        ownership = await self._claim_snapshot(namespace, task_id)
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
            committed = await self.store.add_relation(
                namespace,
                task_id,
                related_namespace=related_namespace,
                related_task_id=related_task_id,
                relation_kind=kind,
                agent_id=agent_id,
                expected_revision=self._effective_revision(kwargs, current),
                expected_claim_ids=ownership,
                capture_task=True,
            )
        except TaskRelationConflict as exc:
            return {
                "ok": False,
                "code": exc.code,
                "error": f"task.relate: {exc}",
                "warnings": [],
            }
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": f"task.relate: {exc}", "warnings": []}
        return await self._result(namespace, task_id, [], committed_task=committed)

    async def _action_unrelate(self, agent_id, namespace, task_id, **kwargs):
        current = await self._required(namespace, task_id)
        if not current:
            return self._missing()
        ownership = await self._claim_snapshot(namespace, task_id)
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
        committed = await self.store.remove_relation(
            namespace,
            task_id,
            related_namespace=related_namespace,
            related_task_id=related_task_id,
            relation_kind=kind,
            agent_id=agent_id,
            expected_revision=self._effective_revision(kwargs, current),
            expected_claim_ids=ownership,
            capture_task=True,
        )
        return await self._result(namespace, task_id, [], committed_task=committed)

    async def _action_update(self, agent_id, namespace, task_id, **kwargs):
        if kwargs.get("state") is not None:
            return public_error(
                "input_validation_failed",
                reason="constraint_violation",
                path="state",
                details=ValidationRepair(
                    validation_errors=(
                        ValidationIssue(
                            error_class="invalid_value",
                            path="state",
                            description="Change workflow state with action=state or action=done.",
                        ),
                    )
                ),
            ).as_dict()
        if not await self._required(namespace, task_id):
            return self._missing()
        return await self._update_from_kwargs(agent_id, namespace, task_id, kwargs)

    async def _action_state(self, agent_id, namespace, task_id, **kwargs):
        if kwargs.get("state") is None:
            return {"ok": False, "error": "task.state: state required", "warnings": []}
        return await self._update_from_kwargs(agent_id, namespace, task_id, kwargs)

    async def _action_done(self, agent_id, namespace, task_id, **kwargs):
        current = await self._required(namespace, task_id)
        if not current:
            return self._missing()
        if current["state"] != "done" and not self._valid_result(kwargs.get("result")):
            return {"ok": False, "error": "task.done: result required", "warnings": []}
        kwargs["state"] = "done"
        return await self._update_from_kwargs(agent_id, namespace, task_id, kwargs)

    async def _action_archive(self, agent_id, namespace, task_id, **kwargs):
        current = await self._required(namespace, task_id)
        if not current:
            return self._missing()
        ownership = await self._claim_snapshot(namespace, task_id)
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
            self._effective_revision(kwargs, current),
            "archived",
            expected_claim_ids=ownership,
            release_claims_reason="task_archived",
            event_extra={"archive_note": note, "state": current["state"]},
        )

    async def _update_from_kwargs(self, agent_id, namespace, task_id, kwargs):
        dependency_error = self._validate_dependencies(kwargs.get("dependencies"))
        if dependency_error:
            return {"ok": False, "error": dependency_error, "warnings": []}
        current = await self.store.get_task(namespace, task_id)
        if not current:
            return self._missing()
        try:
            if "input_refs" in kwargs:
                kwargs["input_refs"] = self._normalize_refs(kwargs["input_refs"], "input_refs")
            if "output_refs" in kwargs:
                kwargs["output_refs"] = self._normalize_refs(kwargs["output_refs"], "output_refs")
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "warnings": []}
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
            "input_refs": "input_refs",
            "output_refs": "output_refs",
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
        if "candidate_ref" in fields:
            if current["state"] == "done":
                return {
                    "ok": False,
                    "code": "candidate_ref_frozen",
                    "error": "task.update: candidate_ref is immutable after task completion",
                    "warnings": [],
                }
            review_relations = [
                relation
                for relation in await self.store.relations(namespace, task_id)
                if relation["direction"] == "incoming" and relation["kind"] == "review_of"
            ]
            if review_relations:
                return {
                    "ok": False,
                    "code": "candidate_ref_frozen",
                    "error": "task.update: candidate_ref is frozen while review_of relation exists",
                    "warnings": [],
                }
        ownership = await self._claim_snapshot(namespace, task_id)
        claims = await self._live_claims(namespace, task_id)
        owner = claims[0]["agent_id"] if claims else None
        unsafe_fields = set(fields) - SAFE_PARTICIPANT_FIELDS
        if fields.get("state") == current.get("state"):
            unsafe_fields.discard("state")
        workflow_change = bool(unsafe_fields or kwargs.get("dependencies") is not None)
        if workflow_change and owner != agent_id:
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

        dependency_override = None
        if target_state == "done" and current["state"] != "done":
            review_error = await self._review_requirements_error(
                current,
                proposed_output_refs=fields.get("output_refs"),
            )
            if review_error:
                return review_error
            candidate_error = await self._review_completion_candidate_error(
                current,
                proposed_candidate=fields.get("candidate_ref"),
            )
            if candidate_error:
                return candidate_error
            warnings, dependency_override, dependency_failure = await self._dependency_gate(
                namespace,
                task_id,
                operation="done",
                force=bool(kwargs.get("force")),
                force_reason=kwargs.get("force_reason"),
                warnings=warnings,
            )
            if dependency_failure:
                return dependency_failure

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
        if dependency_override is not None:
            additional_events.append(
                {
                    "namespace": namespace,
                    "task_id": task_id,
                    "event_type": "dependency_override",
                    "agent_id": agent_id,
                    "payload": dependency_override,
                }
            )
        if target_state in {"done", "blocked"}:
            additional_events.extend(
                await self._review_feedback_events(
                    current,
                    agent_id=agent_id,
                    outcome=target_state,
                    result=kwargs.get("result"),
                    blocker_reason=blocker_reason,
                    now=utc_text(),
                )
            )
        return await self._update(
            agent_id,
            namespace,
            task_id,
            fields,
            self._effective_revision(kwargs, current),
            "updated",
            warnings=warnings,
            expected_claim_ids=ownership if workflow_change else None,
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
        expected_claim_ids=None,
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
            "input_refs",
            "output_refs",
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
            committed = await self.store.update_task_mutation(
                namespace,
                task_id,
                expected_revision=expected_revision,
                expected_claim_ids=expected_claim_ids,
                dependencies=dependencies,
                event_type=event_type,
                event_agent_id=agent_id,
                event_payload=event_payload,
                release_claims_reason=release_claims_reason,
                additional_events=additional_events,
                now=now,
                **fields,
            )
        except TaskOwnershipConflict:
            return self._ownership_conflict_result()
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
                namespace,
                task_id,
                warnings,
                ok=False,
                error="revision conflict",
                code="revision_conflict",
                details={"current_revision": exc.actual},
            )
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": f"task.update: {exc}", "warnings": warnings}
        except Exception as exc:
            return {"ok": False, "error": f"task.update: {exc}", "warnings": warnings}
        return await self._result(
            namespace,
            task_id,
            warnings,
            committed_task=committed,
            include_description="description" in fields,
        )

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
        if kwargs.get("candidate_ref") is not None:
            return {
                "ok": False,
                "error": (
                    "task.review: candidate_ref is not accepted; review binds current "
                    "output_state_id"
                ),
                "warnings": [],
            }
        output_state_id = current.get("output_state_id")
        if output_state_id is None:
            return {
                "ok": False,
                "code": "output_state_missing",
                "error": "task.review: task has no current output state",
                "warnings": [],
            }

        warnings = []
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
        output_refs = list(current.get("output_refs") or [])
        payload = {
            "output_state_id": output_state_id,
            "output_refs": output_refs,
            "dimensions": dimensions,
            "verdict": verdict,
            "evidence": kwargs.get("evidence") or {},
            "warnings": [item["code"] for item in warnings],
        }
        try:
            committed = await self.store.upsert_reviews(
                namespace,
                task_id,
                output_state_id=output_state_id,
                output_refs=output_refs,
                dimensions=dimensions,
                verdict=verdict,
                agent_id=agent_id,
                evidence=kwargs.get("evidence") or {},
                warnings=warnings,
                now=now,
                expected_revision=self._effective_revision(kwargs, current),
                capture_task=True,
                event_payload=payload,
            )
        except ValueError as exc:
            if "changed before review" in str(exc):
                return {
                    "ok": False,
                    "code": "output_state_changed",
                    "error": f"task.review: {exc}",
                    "warnings": warnings,
                }
            raise
        self._inc("terminal_mcp_task_reviews_total")
        return await self._result(namespace, task_id, warnings, committed_task=committed)

    async def _required(self, namespace, task_id):
        return await self.store.get_task(namespace, task_id) if task_id else None

    @staticmethod
    def _missing():
        return {"ok": False, "code": "task_not_found", "error": "task not found", "warnings": []}

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
        stale_claims = len(await self.store.stale_leased_claims())
        now = utc_now()
        for claim in await self.store.all_active_claims():
            if claim.get("owner_kind", "legacy_session") == "logical_agent":
                continue
            session = (
                await self.agent_store.get_session(claim.get("owner_id") or claim["agent_id"])
                if self.agent_store
                else None
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
        result = await self.store.release_owner_claims_mutation(
            owner=ClaimOwner.legacy_session(agent_id), reason=reason, now=now
        )
        return result["released_count"]

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
                    "operational_status": item["state"],
                    "isolation_hint": item["isolation_hint"],
                }
            )
        return result

    async def record_command(
        self,
        agent_id,
        command_hash,
        command_type,
        task_refs,
        *,
        logical_agent_id=None,
        work_session_id=None,
        session_epoch=None,
    ):
        now = utc_text()
        for item in task_refs:
            await self.store.add_event(
                item["namespace"],
                item["task_id"],
                "command",
                agent_id=agent_id,
                payload={"command_hash": command_hash, "command_type": command_type},
                now=now,
                logical_agent_id=logical_agent_id,
                work_session_id=work_session_id,
                session_epoch=session_epoch,
            )

    async def _result(
        self,
        namespace,
        task_id,
        warnings,
        *,
        ok=True,
        error=None,
        code=None,
        details=None,
        committed_task=None,
        include_description=False,
    ):
        task = (
            committed_task
            if committed_task is not None
            else await self.store.get_task(namespace, task_id)
        )
        if isinstance(task, TaskCommittedRecord):
            # A committed mutation is reported from its transaction snapshot.
            # No post-commit database read or external projection can mask it.
            projected = self._decorate_prefetched(
                task,
                task.claims,
                task.dependencies,
                task.sessions_by_agent,
                now=task.observed_at,
            )
            # Exact mutation receipts come from the captured write transaction.
            # Preserve the committed revision and materialize mandatory public
            # claim snapshot fields from that SAME committed record, never by
            # issuing a post-commit list/read that can race another writer.
            description = task.get("description") or ""
            projected["description_preview"] = description[:DESCRIPTION_PREVIEW_LIMIT]
            projected["description_truncated"] = len(description) > DESCRIPTION_PREVIEW_LIMIT
            # The claim has been captured with the committed TaskRecord; a
            # post-commit read could observe a later owner's revision instead.
            projected["claim"] = projected.get("owner")
            if include_description:
                projected["description"] = description
        else:
            projected = await self._decorate(task, details=False) if task else None
        for warning in warnings:
            self._inc("terminal_mcp_task_warnings_total", (("code", warning["code"]),))
        result = {
            "ok": ok,
            "task": projected,
            "warnings": warnings,
            "error": error,
        }
        if code is not None:
            result["code"] = code
        if details is not None:
            result["details"] = details
        return result
