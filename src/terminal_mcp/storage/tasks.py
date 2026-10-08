# ruff: noqa: E501
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.storage.sqlite_observability import cancellation_safe_connection

LANES = frozenset({"implementation", "review", "release", "integration", "general"})
STATES = frozenset({"ready", "in_progress", "blocked", "deferred", "done"})
REVIEW_DIMENSIONS = frozenset({"A", "C", "R"})


class TaskClaimConflict(RuntimeError):
    def __init__(self, namespace: str, task_id: str, agent_ids: list[str]):
        self.namespace = namespace
        self.task_id = task_id
        self.agent_ids = agent_ids
        super().__init__(f"task already claimed: {namespace}/{task_id}")


class TaskAgentBusy(RuntimeError):
    def __init__(
        self,
        namespace: str,
        task_id: str,
        *,
        claimed_at: str,
        claim_intent: str,
    ):
        self.namespace = namespace
        self.task_id = task_id
        self.claimed_at = claimed_at
        self.claim_intent = claim_intent
        super().__init__(f"owner already has a live claim: {namespace}/{task_id}")


class TaskRevisionConflict(RuntimeError):
    def __init__(self, namespace: str, task_id: str, expected: int, actual: int):
        self.namespace = namespace
        self.task_id = task_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"task revision conflict for {namespace}/{task_id}: expected {expected}, actual {actual}"
        )


class TaskOwnershipConflict(RuntimeError):
    """A workflow mutation lost the ownership snapshot authorized at preflight."""

    def __init__(self, namespace: str, task_id: str):
        self.namespace = namespace
        self.task_id = task_id
        super().__init__(f"Task ownership changed before mutation: {namespace}/{task_id}")


class TaskRelationConflict(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class TaskCommittedRecord(dict):
    """Dict-compatible task record and ownership captured by its write transaction.

    Auxiliary snapshots are Python attributes, never extra public task fields.
    Callers receive this object only after a successful commit.
    """

    def __init__(
        self,
        task: dict,
        claims: list[dict],
        dependencies: list[dict],
        sessions_by_agent: dict | None = None,
    ):
        super().__init__(task)
        self.claims = claims
        self.dependencies = dependencies
        self.sessions_by_agent = sessions_by_agent or {}
        self.observed_at = utc_now()


class TaskStore:
    def __init__(self, path):
        self.path = path

    @asynccontextmanager
    async def _connect(self):
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            await db.execute("PRAGMA busy_timeout=1000")
            await db.execute("PRAGMA foreign_keys=ON")
            yield db

    @staticmethod
    async def _assert_task_revision(db, namespace, task_id, expected_revision):
        """Check the task-record precondition inside the caller's write transaction."""
        if expected_revision is None:
            return
        current = await (
            await db.execute(
                "SELECT revision FROM work_items WHERE namespace=? AND task_id=?",
                (namespace, task_id),
            )
        ).fetchone()
        if current is None:
            raise KeyError(f"unknown task: {namespace}/{task_id}")
        if int(current[0]) != int(expected_revision):
            raise TaskRevisionConflict(namespace, task_id, int(expected_revision), int(current[0]))

    @staticmethod
    async def _assert_claim_snapshot(db, namespace, task_id, expected_claim_ids):
        if expected_claim_ids is None:
            return
        rows = await (
            await db.execute(
                "SELECT id FROM work_claims WHERE namespace=? AND task_id=? "
                "AND released_at IS NULL ORDER BY claimed_at,id",
                (namespace, task_id),
            )
        ).fetchall()
        if tuple(row[0] for row in rows) != tuple(expected_claim_ids):
            raise TaskOwnershipConflict(namespace, task_id)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _loads(value: str | None, fallback):
        if value is None:
            return fallback
        return json.loads(value)

    @staticmethod
    def _validate_lane(lane: str):
        if lane not in LANES:
            raise ValueError(f"unsupported lane: {lane}")

    @staticmethod
    def _validate_state(state: str):
        if state not in STATES:
            raise ValueError(f"unsupported state: {state}")

    @staticmethod
    def _normalize_dependencies(namespace: str, dependencies):
        normalized = []
        for item in dependencies or []:
            if isinstance(item, str):
                dep_namespace, dep_task_id = namespace, item
            elif isinstance(item, dict):
                dep_namespace = item.get("namespace") or namespace
                dep_task_id = item.get("task_id")
                if not isinstance(dep_task_id, str) or not dep_task_id.strip():
                    raise ValueError("dependency task_id is required")
            else:
                dep_namespace, dep_task_id = item
            normalized.append((dep_namespace, dep_task_id))
        return normalized

    @staticmethod
    def _normalize_refs(refs):
        if refs is None:
            return []
        if not isinstance(refs, list):
            raise ValueError("refs must be a list")
        if len(refs) > 64:
            raise ValueError("refs maximum is 64")
        result = []
        seen = set()
        for index, value in enumerate(refs):
            if not isinstance(value, str):
                raise ValueError(f"ref[{index}] must be a string")
            if not value:
                raise ValueError(f"ref[{index}] must not be empty")
            if value != value.strip():
                raise ValueError(f"ref[{index}] must not have leading/trailing whitespace")
            if len(value) > 512:
                raise ValueError(f"ref[{index}] maximum length is 512 characters")
            if value not in seen:
                seen.add(value)
                result.append(value)
        return result

    @staticmethod
    def _task(row):
        if row is None:
            return None
        return {
            "namespace": row[0],
            "task_id": row[1],
            "title": row[2],
            "lane": row[3],
            "priority": row[4],
            "state": row[5],
            "description": row[6],
            "next_action": row[7],
            "resource": TaskStore._loads(row[8], {}),
            "reviews": TaskStore._loads(row[9], []),
            "cooperative": bool(row[10]),
            "checkpoint": TaskStore._loads(row[11], {}),
            "candidate_ref": row[12],
            "result": TaskStore._loads(row[13], None),
            "tags": TaskStore._loads(row[14], []),
            "state_changed_at": row[15],
            "ready_since": row[16],
            "archived_at": row[17],
            "archive_note": row[18],
            "revision": row[19],
            "created_at": row[20],
            "updated_at": row[21],
            "isolation_hint": row[22],
            "input_refs": TaskStore._loads(row[23], []),
            "output_refs": TaskStore._loads(row[24], []),
            "output_state_id": row[25],
        }

    async def create_task(
        self,
        namespace: str,
        task_id: str,
        title: str,
        *,
        lane: str = "general",
        priority: int = 0,
        state: str = "ready",
        description: str = "",
        next_action: str = "",
        isolation_hint: str = "none",
        resource: Any = None,
        reviews: Any = None,
        cooperative: bool = False,
        checkpoint: Any = None,
        candidate_ref: str | None = None,
        input_refs: Any = None,
        output_refs: Any = None,
        result: Any = None,
        tags: Any = None,
        now: str | None = None,
    ):
        if not namespace or not task_id or not title:
            raise ValueError("namespace, task_id and title are required")
        self._validate_lane(lane)
        self._validate_state(state)
        now = now or utc_text()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO work_items(namespace,task_id,title,lane,priority,state,description,next_action,isolation_hint,"
                "resource_json,reviews_json,cooperative,checkpoint_json,candidate_ref,result_json,tags_json,state_changed_at,ready_since,revision,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                (
                    namespace,
                    task_id,
                    title,
                    lane,
                    int(priority),
                    state,
                    description,
                    next_action,
                    isolation_hint,
                    self._json(resource or {}),
                    self._json(reviews or []),
                    int(bool(cooperative)),
                    self._json(checkpoint or {}),
                    candidate_ref,
                    self._json(result) if result is not None else None,
                    self._json(tags or []),
                    now,
                    now if state == "ready" else None,
                    now,
                    now,
                ),
            )
            input_refs = self._normalize_refs(input_refs)
            if output_refs is None and candidate_ref:
                output_refs = [candidate_ref]
            output_refs = self._normalize_refs(output_refs)
            cursor = await db.execute(
                "INSERT INTO work_output_states(namespace,task_id,output_refs_json,created_at) "
                "VALUES(?,?,?,?)",
                (namespace, task_id, self._json(output_refs), now),
            )
            output_state_id = int(cursor.lastrowid)
            await db.execute(
                "UPDATE work_items SET input_refs_json=?,output_refs_json=?,output_state_id=? "
                "WHERE namespace=? AND task_id=?",
                (
                    self._json(input_refs),
                    self._json(output_refs),
                    output_state_id,
                    namespace,
                    task_id,
                ),
            )
            await db.commit()
        return await self.get_task(namespace, task_id)

    async def create_task_mutation(
        self,
        namespace: str,
        task_id: str,
        title: str,
        *,
        lane: str = "general",
        priority: int = 0,
        state: str = "ready",
        description: str = "",
        next_action: str = "",
        isolation_hint: str = "none",
        resource: Any = None,
        reviews: Any = None,
        cooperative: bool = False,
        checkpoint: Any = None,
        candidate_ref: str | None = None,
        input_refs: Any = None,
        output_refs: Any = None,
        result: Any = None,
        tags: Any = None,
        dependencies=None,
        event_agent_id: str | None = None,
        event_payload: Any = None,
        dependency_override: Any = None,
        now: str | None = None,
    ):
        if not namespace or not task_id or not title:
            raise ValueError("namespace, task_id and title are required")
        self._validate_lane(lane)
        self._validate_state(state)
        normalized = self._normalize_dependencies(namespace, dependencies)
        now = now or utc_text()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._validate_dependency_graph_tx(db, namespace, task_id, normalized)
                await db.execute(
                    "INSERT INTO work_items(namespace,task_id,title,lane,priority,state,description,next_action,isolation_hint,"
                    "resource_json,reviews_json,cooperative,checkpoint_json,candidate_ref,result_json,tags_json,state_changed_at,ready_since,revision,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                    (
                        namespace,
                        task_id,
                        title,
                        lane,
                        int(priority),
                        state,
                        description,
                        next_action,
                        isolation_hint,
                        self._json(resource or {}),
                        self._json(reviews or []),
                        int(bool(cooperative)),
                        self._json(checkpoint or {}),
                        candidate_ref,
                        self._json(result) if result is not None else None,
                        self._json(tags or []),
                        now,
                        now if state == "ready" else None,
                        now,
                        now,
                    ),
                )
                input_refs = self._normalize_refs(input_refs)
                output_refs = self._normalize_refs(output_refs)
                cursor = await db.execute(
                    "INSERT INTO work_output_states(namespace,task_id,output_refs_json,created_at) "
                    "VALUES(?,?,?,?)",
                    (namespace, task_id, self._json(output_refs), now),
                )
                output_state_id = int(cursor.lastrowid)
                await db.execute(
                    "UPDATE work_items SET input_refs_json=?,output_refs_json=?,output_state_id=? "
                    "WHERE namespace=? AND task_id=?",
                    (
                        self._json(input_refs),
                        self._json(output_refs),
                        output_state_id,
                        namespace,
                        task_id,
                    ),
                )
                if dependencies is not None:
                    await db.executemany(
                        "INSERT INTO work_dependencies(namespace,task_id,dependency_namespace,dependency_task_id,created_at) "
                        "VALUES(?,?,?,?,?)",
                        [
                            (namespace, task_id, dep_ns, dep_id, now)
                            for dep_ns, dep_id in normalized
                        ],
                    )
                await db.execute(
                    "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        namespace,
                        task_id,
                        "created",
                        event_agent_id,
                        self._json(
                            {
                                **(event_payload or {}),
                                "input_refs": input_refs,
                                "output_refs": output_refs,
                                "output_state_id": output_state_id,
                            }
                        ),
                        now,
                    ),
                )
                if dependency_override:
                    await db.execute(
                        "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (
                            namespace,
                            task_id,
                            "dependency_override",
                            event_agent_id,
                            self._json(dependency_override),
                            now,
                        ),
                    )
                committed = await self._committed_record_tx(db, namespace, task_id)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return committed

    async def _get_task_tx(self, db, namespace: str, task_id: str):
        row = await (
            await db.execute(
                "SELECT namespace,task_id,title,lane,priority,state,description,next_action,resource_json,"
                "reviews_json,cooperative,checkpoint_json,candidate_ref,result_json,tags_json,state_changed_at,ready_since,archived_at,archive_note,revision,created_at,updated_at,isolation_hint,input_refs_json,output_refs_json,output_state_id "
                "FROM work_items WHERE namespace=? AND task_id=?",
                (namespace, task_id),
            )
        ).fetchone()
        return self._task(row)

    async def _committed_record_tx(self, db, namespace: str, task_id: str):
        task = await self._get_task_tx(db, namespace, task_id)
        if task is None:
            raise KeyError(f"unknown task: {namespace}/{task_id}")
        rows = await (
            await db.execute(
                "SELECT id,agent_id,owner_kind,owner_id,claimed_at,released_at,claim_intent "
                "FROM work_claims WHERE namespace=? AND task_id=? AND released_at IS NULL "
                "ORDER BY claimed_at,id",
                (namespace, task_id),
            )
        ).fetchall()
        dependencies = await (
            await db.execute(
                "SELECT d.dependency_namespace,d.dependency_task_id,w.state,w.archived_at "
                "FROM work_dependencies d LEFT JOIN work_items w "
                "ON w.namespace=d.dependency_namespace AND w.task_id=d.dependency_task_id "
                "WHERE d.namespace=? AND d.task_id=? "
                "ORDER BY d.dependency_namespace,d.dependency_task_id",
                (namespace, task_id),
            )
        ).fetchall()
        session_fields = (
            "agent_id",
            "registered_at",
            "last_activity_at",
            "state",
            "global_expires_at",
        )
        sessions = await (
            await db.execute(
                "SELECT DISTINCT s.agent_id,s.registered_at,s.last_activity_at,s.state,"
                "s.global_expires_at FROM agent_sessions s JOIN work_claims c "
                "ON s.agent_id=c.owner_id WHERE c.namespace=? AND c.task_id=? "
                "AND c.released_at IS NULL AND c.owner_kind='legacy_session'",
                (namespace, task_id),
            )
        ).fetchall()
        return TaskCommittedRecord(
            task,
            [self._claim_record(row) for row in rows],
            [
                {
                    "namespace": row[0],
                    "task_id": row[1],
                    "state": row[2] or "missing",
                    "archived": bool(row[3]),
                    "satisfied": row[2] == "done",
                }
                for row in dependencies
            ],
            {row[0]: dict(zip(session_fields, row, strict=True)) for row in sessions},
        )

    async def get_task(self, namespace: str, task_id: str):
        async with self._connect() as db:
            return await self._get_task_tx(db, namespace, task_id)

    async def list_tasks(
        self,
        *,
        namespace: str | None = None,
        lane: str | None = None,
        state: str | None = None,
        tags=None,
        show_done: bool = False,
        show_archived: bool = False,
        limit: int | None = 100,
        offset: int = 0,
    ):
        where = []
        params: list[Any] = []
        if namespace is not None:
            where.append("namespace=?")
            params.append(namespace)
        if lane is not None:
            self._validate_lane(lane)
            where.append("lane=?")
            params.append(lane)
        if state is not None:
            self._validate_state(state)
            where.append("state=?")
            params.append(state)
        if not show_archived:
            where.append("archived_at IS NULL")
        if not show_done:
            if show_archived:
                where.append("(state<>'done' OR archived_at IS NOT NULL)")
            else:
                where.append("state<>'done'")
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        query = (
            "SELECT namespace,task_id,title,lane,priority,state,description,next_action,resource_json,"
            "reviews_json,cooperative,checkpoint_json,candidate_ref,result_json,tags_json,"
            "state_changed_at,ready_since,archived_at,archive_note,revision,created_at,updated_at,isolation_hint,input_refs_json,output_refs_json,output_state_id "
            f"FROM work_items{clause} "
            "ORDER BY priority DESC,"
            "CASE WHEN state='ready' THEN 0 ELSE 1 END,"
            "COALESCE(ready_since,created_at) ASC,updated_at DESC,namespace,task_id"
        )
        required_tags = set(tags or [])
        offset = max(0, int(offset))
        if not required_tags and limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend((max(1, min(int(limit), 1000)), offset))
            async with self._connect() as db:
                rows = await (await db.execute(query, params)).fetchall()
            return [self._task(row) for row in rows]
        async with self._connect() as db:
            rows = await (await db.execute(query, params)).fetchall()
        tasks = [self._task(row) for row in rows]
        if required_tags:
            tasks = [task for task in tasks if required_tags.issubset(set(task["tags"]))]
        if limit is None:
            return tasks[offset:]
        return tasks[offset : offset + max(1, min(int(limit), 1000))]

    async def list_namespaces(self, *, limit: int = 20, offset: int = 0) -> list[str]:
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT DISTINCT namespace FROM work_items ORDER BY namespace LIMIT ? OFFSET ?",
                    (max(1, min(int(limit), 1000)), max(0, int(offset))),
                )
            ).fetchall()
        return [str(row[0]) for row in rows]

    async def runtime_state_snapshot(self):
        # Batch claims and dependency state for task list projection.
        async with self._connect() as db:
            claim_rows = await (
                await db.execute(
                    "SELECT id,namespace,task_id,agent_id,owner_kind,owner_id,claimed_at,claim_intent "
                    "FROM work_claims WHERE released_at IS NULL ORDER BY claimed_at,id"
                )
            ).fetchall()
            dependency_rows = await (
                await db.execute(
                    "SELECT d.namespace,d.task_id,d.dependency_namespace,d.dependency_task_id,"
                    "d.created_at,w.state,w.archived_at "
                    "FROM work_dependencies d "
                    "LEFT JOIN work_items w ON w.namespace=d.dependency_namespace "
                    "AND w.task_id=d.dependency_task_id "
                    "ORDER BY d.namespace,d.task_id,d.dependency_namespace,d.dependency_task_id"
                )
            ).fetchall()

        claims = {}
        for row in claim_rows:
            claims.setdefault((row[1], row[2]), []).append(
                {
                    "id": row[0],
                    "agent_id": row[3],
                    "owner_kind": row[4],
                    "owner_id": row[5],
                    "claimed_at": row[6],
                    "claim_intent": row[7],
                }
            )
        dependencies = {}
        for row in dependency_rows:
            state = row[5] if row[5] is not None else "missing"
            dependencies.setdefault((row[0], row[1]), []).append(
                {
                    "namespace": row[2],
                    "task_id": row[3],
                    "created_at": row[4],
                    "state": state,
                    "archived": bool(row[6]) if row[5] is not None else False,
                    "satisfied": state == "done",
                }
            )
        return {"claims": claims, "dependencies": dependencies}

    async def update_task(
        self,
        namespace: str,
        task_id: str,
        *,
        expected_revision: int | None = None,
        now: str | None = None,
        **changes,
    ):
        allowed = {
            "title",
            "lane",
            "priority",
            "state",
            "description",
            "next_action",
            "resource",
            "reviews",
            "cooperative",
            "checkpoint",
            "candidate_ref",
            "input_refs",
            "output_refs",
            "result",
            "tags",
            "archived_at",
            "archive_note",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported task fields: {sorted(unknown)}")
        if not changes:
            return await self.get_task(namespace, task_id)
        if "input_refs" in changes:
            changes["input_refs"] = self._normalize_refs(changes["input_refs"])
        if "output_refs" in changes:
            changes["output_refs"] = self._normalize_refs(changes["output_refs"])
        if "input_refs" in changes or "output_refs" in changes:
            return await self.update_task_mutation(
                namespace,
                task_id,
                expected_revision=expected_revision,
                event_type="updated",
                event_payload={"fields": sorted(changes)},
                now=now,
                **changes,
            )
        if "lane" in changes:
            self._validate_lane(changes["lane"])
        if "state" in changes:
            self._validate_state(changes["state"])
        columns = {
            "resource": "resource_json",
            "reviews": "reviews_json",
            "checkpoint": "checkpoint_json",
            "result": "result_json",
            "tags": "tags_json",
            "input_refs": "input_refs_json",
            "output_refs": "output_refs_json",
        }
        assignments = []
        params: list[Any] = []
        for key, value in changes.items():
            column = columns.get(key, key)
            if key in {
                "resource",
                "reviews",
                "checkpoint",
                "result",
                "tags",
                "input_refs",
                "output_refs",
            }:
                if key == "result":
                    value = self._json(value) if value is not None else None
                elif key in {"tags", "input_refs", "output_refs"}:
                    value = self._json(value or [])
                else:
                    value = self._json(
                        value if value is not None else ({} if key != "reviews" else [])
                    )
            elif key == "cooperative":
                value = int(bool(value))
            elif key == "priority":
                value = int(value)
            assignments.append(f"{column}=?")
            params.append(value)
        now = now or utc_text()
        if "state" in changes:
            current = await self.get_task(namespace, task_id)
            if current is None:
                raise KeyError(f"unknown task: {namespace}/{task_id}")
            if changes["state"] != current["state"]:
                assignments.extend(["state_changed_at=?", "ready_since=?"])
                params.extend([now, now if changes["state"] == "ready" else None])
        assignments.extend(["revision=revision+1", "updated_at=?"])
        params.append(now)
        where = "namespace=? AND task_id=?"
        params.extend([namespace, task_id])
        if expected_revision is not None:
            where += " AND revision=?"
            params.append(int(expected_revision))
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                f"UPDATE work_items SET {','.join(assignments)} WHERE {where}", params
            )
            if cur.rowcount != 1:
                row = await (
                    await db.execute(
                        "SELECT revision,state FROM work_items WHERE namespace=? AND task_id=?",
                        (namespace, task_id),
                    )
                ).fetchone()
                await db.rollback()
                if row is None:
                    raise KeyError(f"unknown task: {namespace}/{task_id}")
                if expected_revision is not None:
                    raise TaskRevisionConflict(
                        namespace, task_id, int(expected_revision), int(row[0])
                    )
                raise RuntimeError("task update failed")
            await db.commit()
        return await self.get_task(namespace, task_id)

    async def set_workflow_state(
        self, namespace: str, task_id: str, state: str, *,
        agent_id: str, now: str | None = None,
    ) -> TaskCommittedRecord:
        """Atomically change only workflow state; content CAS revision is independent.

        Ownership is checked under the same write lock as the state transition.
        A repeated assignment is a successful no-op with no workflow event.
        """
        self._validate_state(state)
        now = now or utc_text()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT state,cooperative,archived_at FROM work_items "
                        "WHERE namespace=? AND task_id=?", (namespace, task_id)
                    )
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown task: {namespace}/{task_id}")
                if row[2] is not None:
                    raise ValueError("archived task cannot change state")
                claims = await (
                    await db.execute(
                        "SELECT owner_id FROM work_claims WHERE namespace=? "
                        "AND task_id=? AND released_at IS NULL",
                        (namespace, task_id),
                    )
                ).fetchall()
                if claims and any(claim[0] != agent_id for claim in claims):
                    # All live co-owners may change the workflow independently.
                    # An unclaimed task is writable by any authorized task-state caller.
                    if not bool(row[1]) or agent_id not in {claim[0] for claim in claims}:
                        raise TaskOwnershipConflict(namespace, task_id)
                if row[0] != state:
                    await db.execute(
                        "UPDATE work_items SET state=?,state_changed_at=?,ready_since=?,"
                        "updated_at=? WHERE namespace=? AND task_id=?",
                        (state, now, now if state == "ready" else None,
                         now, namespace, task_id),
                    )
                    await db.execute(
                        "INSERT INTO work_events(namespace,task_id,event_type,agent_id,"
                        "payload_json,created_at) VALUES(?,?,?,?,?,?)",
                        (namespace, task_id, "updated", agent_id,
                         self._json({"fields": ["state"], "state": state}), now),
                    )
                committed = await self._committed_record_tx(db, namespace, task_id)
                await db.commit()
                return committed
            except Exception:
                await db.rollback()
                raise

    async def update_task_mutation(
        self,
        namespace: str,
        task_id: str,
        *,
        expected_revision: int | None = None,
        expected_claim_ids: tuple[int, ...] | None = None,
        dependencies=None,
        event_type: str = "updated",
        event_agent_id: str | None = None,
        event_payload: Any = None,
        release_claims_reason: str | None = None,
        additional_events=None,
        now: str | None = None,
        **changes,
    ):
        allowed = {
            "title",
            "lane",
            "priority",
            "state",
            "description",
            "next_action",
            "resource",
            "reviews",
            "cooperative",
            "checkpoint",
            "candidate_ref",
            "input_refs",
            "output_refs",
            "result",
            "tags",
            "archived_at",
            "archive_note",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported task fields: {sorted(unknown)}")
        if "lane" in changes:
            self._validate_lane(changes["lane"])
        if "state" in changes:
            self._validate_state(changes["state"])
        if "input_refs" in changes:
            changes["input_refs"] = self._normalize_refs(changes["input_refs"])
        if "output_refs" in changes:
            changes["output_refs"] = self._normalize_refs(changes["output_refs"])
        normalized = self._normalize_dependencies(namespace, dependencies)
        columns = {
            "resource": "resource_json",
            "reviews": "reviews_json",
            "checkpoint": "checkpoint_json",
            "result": "result_json",
            "tags": "tags_json",
            "input_refs": "input_refs_json",
            "output_refs": "output_refs_json",
        }
        assignments = []
        params: list[Any] = []
        for key, value in changes.items():
            column = columns.get(key, key)
            if key in {
                "resource",
                "reviews",
                "checkpoint",
                "result",
                "tags",
                "input_refs",
                "output_refs",
            }:
                if key == "result":
                    value = self._json(value) if value is not None else None
                elif key in {"tags", "input_refs", "output_refs"}:
                    value = self._json(value or [])
                else:
                    value = self._json(
                        value if value is not None else ({} if key != "reviews" else [])
                    )
            elif key == "cooperative":
                value = int(bool(value))
            elif key == "priority":
                value = int(value)
            assignments.append(f"{column}=?")
            params.append(value)
        now = now or utc_text()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_claim_snapshot(db, namespace, task_id, expected_claim_ids)
                exists = await (
                    await db.execute(
                        "SELECT revision,state,input_refs_json,output_refs_json,output_state_id "
                        "FROM work_items WHERE namespace=? AND task_id=?",
                        (namespace, task_id),
                    )
                ).fetchone()
                if exists is None:
                    raise KeyError(f"unknown task: {namespace}/{task_id}")
                if expected_revision is not None and int(exists[0]) != int(expected_revision):
                    raise TaskRevisionConflict(namespace, task_id, int(expected_revision), int(exists[0]))
                current_task = await self._get_task_tx(db, namespace, task_id)
                unchanged = all(current_task.get(key) == value for key, value in changes.items())
                if unchanged and dependencies is None and not release_claims_reason and not additional_events:
                    committed = await self._committed_record_tx(db, namespace, task_id)
                    await db.commit()
                    return committed
                previous_input_refs = self._loads(exists[2], [])
                previous_output_refs = self._loads(exists[3], [])
                previous_output_state_id = exists[4]
                input_refs_changed = (
                    "input_refs" in changes and changes["input_refs"] != previous_input_refs
                )
                output_refs_changed = (
                    "output_refs" in changes and changes["output_refs"] != previous_output_refs
                )
                new_output_state_id = previous_output_state_id
                if output_refs_changed:
                    cursor = await db.execute(
                        "INSERT INTO work_output_states(namespace,task_id,output_refs_json,created_at) "
                        "VALUES(?,?,?,?)",
                        (namespace, task_id, self._json(changes["output_refs"]), now),
                    )
                    new_output_state_id = int(cursor.lastrowid)
                    assignments.append("output_state_id=?")
                    params.append(new_output_state_id)
                if assignments:
                    if "state" in changes and changes["state"] != exists[1]:
                        assignments.extend(["state_changed_at=?", "ready_since=?"])
                        params.extend([now, now if changes["state"] == "ready" else None])
                    assignments.extend(["revision=revision+1", "updated_at=?"])
                    update_params = [*params, now, namespace, task_id]
                    where = "namespace=? AND task_id=?"
                    if expected_revision is not None:
                        where += " AND revision=?"
                        update_params.append(int(expected_revision))
                    cur = await db.execute(
                        f"UPDATE work_items SET {','.join(assignments)} WHERE {where}",
                        update_params,
                    )
                    if cur.rowcount != 1:
                        current = await (
                            await db.execute(
                                "SELECT revision FROM work_items WHERE namespace=? AND task_id=?",
                                (namespace, task_id),
                            )
                        ).fetchone()
                        if expected_revision is not None and current is not None:
                            raise TaskRevisionConflict(
                                namespace, task_id, int(expected_revision), int(current[0])
                            )
                        raise RuntimeError("task update failed")
                if dependencies is not None:
                    await self._validate_dependency_graph_tx(db, namespace, task_id, normalized)
                    await db.execute(
                        "DELETE FROM work_dependencies WHERE namespace=? AND task_id=?",
                        (namespace, task_id),
                    )
                    await db.executemany(
                        "INSERT INTO work_dependencies(namespace,task_id,dependency_namespace,dependency_task_id,created_at) "
                        "VALUES(?,?,?,?,?)",
                        [
                            (namespace, task_id, dep_ns, dep_id, now)
                            for dep_ns, dep_id in normalized
                        ],
                    )
                stored_event_payload = dict(event_payload or {})
                if "checkpoint" in stored_event_payload:
                    stored_event_payload["revision"] = int(exists[0]) + 1
                await db.execute(
                    "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        namespace,
                        task_id,
                        event_type,
                        event_agent_id,
                        self._json(stored_event_payload),
                        now,
                    ),
                )
                if input_refs_changed:
                    await db.execute(
                        "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (
                            namespace,
                            task_id,
                            "input_refs_updated",
                            event_agent_id,
                            self._json({"input_refs": changes["input_refs"]}),
                            now,
                        ),
                    )
                if output_refs_changed:
                    await db.execute(
                        "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (
                            namespace,
                            task_id,
                            "output_refs_updated",
                            event_agent_id,
                            self._json(
                                {
                                    "previous_output_state_id": previous_output_state_id,
                                    "new_output_state_id": new_output_state_id,
                                    "previous_output_refs": previous_output_refs,
                                    "new_output_refs": changes["output_refs"],
                                }
                            ),
                            now,
                        ),
                    )
                if dependencies is not None:
                    await db.execute(
                        "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (
                            namespace,
                            task_id,
                            "dependencies_updated",
                            event_agent_id,
                            self._json({"dependencies": dependencies}),
                            now,
                        ),
                    )
                for extra_event in additional_events or []:
                    await db.execute(
                        "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (
                            extra_event["namespace"],
                            extra_event["task_id"],
                            extra_event["event_type"],
                            extra_event.get("agent_id"),
                            self._json(extra_event.get("payload") or {}),
                            extra_event.get("created_at") or now,
                        ),
                    )
                if release_claims_reason:
                    claimants = await (
                        await db.execute(
                            "SELECT agent_id FROM work_claims "
                            "WHERE namespace=? AND task_id=? AND released_at IS NULL ORDER BY id",
                            (namespace, task_id),
                        )
                    ).fetchall()
                    if claimants:
                        await db.execute(
                            "UPDATE work_claims SET released_at=? "
                            "WHERE namespace=? AND task_id=? AND released_at IS NULL",
                            (now, namespace, task_id),
                        )
                        await db.executemany(
                            "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at) "
                            "VALUES(?,?,?,?,?,?)",
                            [
                                (
                                    namespace,
                                    task_id,
                                    "claim_released",
                                    row[0],
                                    self._json({"reason": release_claims_reason}),
                                    now,
                                )
                                for row in claimants
                            ],
                        )
                committed = await self._committed_record_tx(db, namespace, task_id)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return committed

    @staticmethod
    def _claim_record(row, *, include_task=False):
        result = {
            "id": row[0],
            "agent_id": row[1],
            "owner_kind": row[2],
            "owner_id": row[3],
            "claimed_at": row[4],
            "released_at": row[5],
            "claim_intent": row[6],
        }
        if include_task:
            result.update(
                {
                    "namespace": row[7],
                    "task_id": row[8],
                    "lane": row[9],
                    "priority": row[10],
                    "state": row[11],
                    "cooperative": bool(row[12]),
                    "isolation_hint": row[13],
                }
            )
        return result

    async def claims(self, namespace: str, task_id: str, *, active_only: bool = True):
        clause = " AND released_at IS NULL" if active_only else ""
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT id,agent_id,owner_kind,owner_id,claimed_at,released_at,claim_intent FROM work_claims WHERE namespace=? AND task_id=?"
                    + clause
                    + " ORDER BY claimed_at,id",
                    (namespace, task_id),
                )
            ).fetchall()
        return [self._claim_record(row) for row in rows]

    async def claims_for_owner(self, owner: ClaimOwner, *, active_only: bool = True):
        clause = " AND c.released_at IS NULL" if active_only else ""
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT c.id,c.agent_id,c.owner_kind,c.owner_id,c.claimed_at,c.released_at,c.claim_intent,c.namespace,c.task_id,w.lane,w.priority,w.state,w.cooperative,w.isolation_hint FROM work_claims c JOIN work_items w ON w.namespace=c.namespace AND w.task_id=c.task_id WHERE c.owner_kind=? AND c.owner_id=?"
                    + clause
                    + " ORDER BY c.claimed_at,c.id",
                    (owner.kind, owner.owner_id),
                )
            ).fetchall()
        return [self._claim_record(row, include_task=True) for row in rows]

    async def claims_for_agent(self, agent_id: str, *, active_only: bool = True):
        return await self.claims_for_owner(
            ClaimOwner.legacy_session(agent_id), active_only=active_only
        )

    async def fence_claim_session(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        reason: str,
        now: str | None = None,
    ):
        """Durable admission fence, serialized against a late remote claim commit."""
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO work_claim_session_fences VALUES(?,?,?,?,?) "
                "ON CONFLICT(logical_agent_id,work_session_id,session_epoch) DO NOTHING",
                (logical_agent_id, work_session_id, session_epoch, now or utc_text(), reason),
            )
            await db.commit()

    async def release_work_session_claims(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        reason: str,
        now: str | None = None,
    ) -> int:
        from terminal_mcp.storage.claim_leases import release_session_claims

        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                count = await release_session_claims(
                    db,
                    logical_agent_id,
                    work_session_id,
                    session_epoch,
                    now=now or utc_text(),
                    reason=reason,
                )
                await db.commit()
                return count
            except Exception:
                await db.rollback()
                raise

    async def expired_claim_sessions(
        self, *, local_authority: str, now: str | None = None
    ) -> list[dict]:
        """Remote leases are recovered even for sessions which ran no command."""
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT DISTINCT l.logical_agent_id,l.work_session_id,l.session_epoch "
                    "FROM work_claim_leases l JOIN work_claims c ON c.id=l.claim_id "
                    "LEFT JOIN logical_agent_work_sessions s ON s.work_session_id=l.work_session_id "
                    "LEFT JOIN work_claim_session_fences f ON f.logical_agent_id=l.logical_agent_id "
                    "AND f.work_session_id=l.work_session_id AND f.session_epoch=l.session_epoch "
                    "WHERE c.released_at IS NULL AND (l.hard_expires_at<=? OR f.revoked_at IS NOT NULL) "
                    "AND (s.authority_node_id IS NULL OR s.authority_node_id<>?) "
                    "ORDER BY l.hard_expires_at,l.work_session_id LIMIT 256",
                    (now or utc_text(), local_authority),
                )
            ).fetchall()
        return [dict(logical_agent_id=r[0], work_session_id=r[1], session_epoch=r[2]) for r in rows]

    async def stale_leased_claims(self, *, now: str | None = None) -> list[dict]:
        """Bounded health diagnostics; observations do not mutate ownership."""
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT c.id,c.namespace,c.task_id,l.logical_agent_id,l.work_session_id,l.session_epoch "
                    "FROM work_claims c JOIN work_claim_leases l ON c.id=l.claim_id "
                    "LEFT JOIN logical_agent_work_sessions s ON s.work_session_id=l.work_session_id "
                    "LEFT JOIN work_claim_session_fences f ON f.logical_agent_id=l.logical_agent_id "
                    "AND f.work_session_id=l.work_session_id AND f.session_epoch=l.session_epoch "
                    "WHERE c.released_at IS NULL AND (l.hard_expires_at<=? "
                    "OR s.state IN ('ended','expired','failed','suspended') OR f.revoked_at IS NOT NULL) "
                    "ORDER BY c.id LIMIT 256",
                    (now or utc_text(),),
                )
            ).fetchall()
        return [
            dict(
                zip(
                    (
                        "id",
                        "namespace",
                        "task_id",
                        "logical_agent_id",
                        "work_session_id",
                        "session_epoch",
                    ),
                    r,
                    strict=True,
                )
            )
            for r in rows
        ]

    async def all_active_claims(self):
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT id,agent_id,owner_kind,owner_id,claimed_at,released_at,claim_intent,namespace,task_id FROM work_claims WHERE released_at IS NULL ORDER BY claimed_at,id"
                )
            ).fetchall()
        result = []
        for row in rows:
            claim = self._claim_record(row[:7])
            claim.update({"namespace": row[7], "task_id": row[8]})
            result.append(claim)
        return result

    async def stats(self):
        async with self._connect() as db:
            states = await (
                await db.execute("SELECT state,COUNT(*) FROM work_items GROUP BY state")
            ).fetchall()
            lanes = await (
                await db.execute(
                    "SELECT lane,COUNT(*) FROM work_items WHERE state<>'done' AND archived_at IS NULL GROUP BY lane"
                )
            ).fetchall()
            active_claims = int(
                (
                    await (
                        await db.execute(
                            "SELECT COUNT(*) FROM work_claims WHERE released_at IS NULL"
                        )
                    ).fetchone()
                )[0]
            )
            reviews = await (
                await db.execute("SELECT verdict,COUNT(*) FROM work_reviews GROUP BY verdict")
            ).fetchall()
        return {
            "by_state": {row[0]: int(row[1]) for row in states},
            "by_lane": {row[0]: int(row[1]) for row in lanes},
            "active_claims": active_claims,
            "reviews": {row[0]: int(row[1]) for row in reviews},
        }

    async def active_claims(self, namespace: str, task_id: str):
        return await self.claims(namespace, task_id, active_only=True)

    async def claim(
        self,
        namespace: str,
        task_id: str,
        agent_id: str,
        *,
        claim_intent: str = "",
        exclusive: bool = False,
        event_payload: Any = None,
        dependency_override: Any = None,
        capture_task: bool = False,
        now: str | None = None,
        expected_revision: int | None = None,
    ):
        return await self.claim_owner(
            namespace,
            task_id,
            ClaimOwner.legacy_session(agent_id),
            claim_intent=claim_intent,
            exclusive=exclusive,
            event_payload=event_payload,
            dependency_override=dependency_override,
            now=now,
            capture_task=capture_task,
            expected_revision=expected_revision,
        )

    async def claim_owner(
        self,
        namespace: str,
        task_id: str,
        owner: ClaimOwner,
        *,
        claim_intent: str = "",
        exclusive: bool = False,
        event_payload: Any = None,
        dependency_override: Any = None,
        capture_task: bool = False,
        now: str | None = None,
        expected_revision: int | None = None,
        lease: dict | None = None,
    ):
        now = now or utc_text()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_task_revision(db, namespace, task_id, expected_revision)
                existing = await (
                    await db.execute(
                        "SELECT id,claimed_at FROM work_claims WHERE namespace=? AND task_id=? AND owner_kind=? AND owner_id=? AND released_at IS NULL",
                        (namespace, task_id, owner.kind, owner.owner_id),
                    )
                ).fetchone()
                if existing:
                    prior_lease = await (
                        await db.execute(
                            "SELECT 1 FROM work_claim_leases WHERE claim_id=?", (existing[0],)
                        )
                    ).fetchone()
                    if lease is not None and prior_lease is not None:
                        from terminal_mcp.storage.claim_leases import attach_claim_lease

                        await attach_claim_lease(db, existing[0], owner.owner_id, lease)
                    await db.execute(
                        "UPDATE work_claims SET claim_intent=? WHERE id=?",
                        (claim_intent, existing[0]),
                    )
                    committed = (
                        await self._committed_record_tx(db, namespace, task_id)
                        if capture_task
                        else None
                    )
                    await db.commit()
                    if capture_task:
                        return committed
                    return {
                        "id": existing[0],
                        "agent_id": owner.owner_id,
                        "owner_kind": owner.kind,
                        "owner_id": owner.owner_id,
                        "claimed_at": existing[1],
                        "claim_intent": claim_intent,
                        "created": False,
                    }
                busy = await (
                    await db.execute(
                        "SELECT namespace,task_id,claimed_at,claim_intent FROM work_claims "
                        "WHERE owner_kind=? AND owner_id=? AND released_at IS NULL "
                        "ORDER BY claimed_at,id LIMIT 1",
                        (owner.kind, owner.owner_id),
                    )
                ).fetchone()
                if busy is not None:
                    raise TaskAgentBusy(
                        busy[0],
                        busy[1],
                        claimed_at=busy[2],
                        claim_intent=busy[3] or "",
                    )
                task = await (
                    await db.execute(
                        "SELECT cooperative,archived_at FROM work_items WHERE namespace=? AND task_id=?",
                        (namespace, task_id),
                    )
                ).fetchone()
                if task is None:
                    raise KeyError(f"unknown task: {namespace}/{task_id}")
                if task[1] is not None:
                    raise ValueError("archived task cannot be claimed")
                if exclusive or not bool(task[0]):
                    conflicts = await (
                        await db.execute(
                            "SELECT owner_id FROM work_claims WHERE namespace=? AND task_id=? AND released_at IS NULL AND NOT (owner_kind=? AND owner_id=?) ORDER BY claimed_at,id",
                            (namespace, task_id, owner.kind, owner.owner_id),
                        )
                    ).fetchall()
                    if conflicts:
                        raise TaskClaimConflict(namespace, task_id, [row[0] for row in conflicts])
                cur = await db.execute(
                    "INSERT INTO work_claims(namespace,task_id,agent_id,claimed_at,released_at,claim_intent,owner_kind,owner_id) VALUES(?,?,?,?,NULL,?,?,?)",
                    (
                        namespace,
                        task_id,
                        owner.owner_id,
                        now,
                        claim_intent,
                        owner.kind,
                        owner.owner_id,
                    ),
                )
                if lease is not None:
                    from terminal_mcp.storage.claim_leases import attach_claim_lease

                    await attach_claim_lease(db, cur.lastrowid, owner.owner_id, lease)
                # Ownership is independent of the agent's explicit workflow state.
                # A session lease must never start a task as a side effect.
                if dependency_override:
                    await db.execute(
                        "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            namespace,
                            task_id,
                            "dependency_override",
                            owner.owner_id,
                            self._json(dependency_override),
                            now,
                        ),
                    )
                payload = dict(event_payload or {})
                payload.update(
                    {
                        "claim_intent": claim_intent,
                        "owner_kind": owner.kind,
                        "owner_id": owner.owner_id,
                    }
                )
                await db.execute(
                    "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                    (namespace, task_id, "claim", owner.owner_id, self._json(payload), now),
                )
                committed = (
                    await self._committed_record_tx(db, namespace, task_id)
                    if capture_task
                    else None
                )
                await db.commit()
                if capture_task:
                    return committed
                return {
                    "id": cur.lastrowid,
                    "agent_id": owner.owner_id,
                    "owner_kind": owner.kind,
                    "owner_id": owner.owner_id,
                    "claimed_at": now,
                    "claim_intent": claim_intent,
                    "created": True,
                }
            except Exception:
                await db.rollback()
                raise

    async def release_claim(
        self, namespace: str, task_id: str, agent_id: str, *, now: str | None = None
    ) -> bool:
        return await self.release_owner_claim(
            namespace, task_id, ClaimOwner.legacy_session(agent_id), now=now
        )

    async def release_owner_claim(
        self, namespace: str, task_id: str, owner: ClaimOwner, *, now: str | None = None
    ) -> bool:
        now = now or utc_text()
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE work_claims SET released_at=? WHERE namespace=? AND task_id=? AND owner_kind=? AND owner_id=? AND released_at IS NULL",
                (now, namespace, task_id, owner.kind, owner.owner_id),
            )
            await db.commit()
        return cur.rowcount > 0

    async def release_claim_mutation(
        self,
        namespace: str,
        task_id: str,
        agent_id: str,
        *,
        reason: str,
        capture_task: bool = False,
        now: str | None = None,
        expected_revision: int | None = None,
        expected_claim_id: int | None = None,
    ) -> bool | TaskCommittedRecord:
        return await self.release_owner_claim_mutation(
            namespace,
            task_id,
            ClaimOwner.legacy_session(agent_id),
            reason=reason,
            now=now,
            capture_task=capture_task,
            expected_revision=expected_revision,
            expected_claim_id=expected_claim_id,
        )

    async def release_owner_claim_mutation(
        self,
        namespace: str,
        task_id: str,
        owner: ClaimOwner,
        *,
        reason: str,
        capture_task: bool = False,
        now: str | None = None,
        expected_revision: int | None = None,
        expected_claim_id: int | None = None,
    ) -> bool | TaskCommittedRecord:
        now = now or utc_text()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_task_revision(db, namespace, task_id, expected_revision)
                leased = await (
                    await db.execute(
                        "SELECT 1 FROM work_claims c JOIN work_claim_leases l ON l.claim_id=c.id "
                        "WHERE c.namespace=? AND c.task_id=? AND c.owner_kind=? AND c.owner_id=? "
                        "AND c.released_at IS NULL LIMIT 1",
                        (namespace, task_id, owner.kind, owner.owner_id),
                    )
                ).fetchone()
                where = (
                    "namespace=? AND task_id=? AND owner_kind=? AND owner_id=? "
                    "AND released_at IS NULL"
                )
                params = [now, namespace, task_id, owner.kind, owner.owner_id]
                if expected_claim_id is not None:
                    # A delayed lifecycle sweep must not release a successor claim.
                    where += " AND id=?"
                    params.append(expected_claim_id)
                cur = await db.execute(
                    f"UPDATE work_claims SET released_at=? WHERE {where}", params
                )
                if cur.rowcount:
                    if leased is not None:
                        from terminal_mcp.storage.claim_leases import refresh_released_task

                        await refresh_released_task(db, namespace, task_id, now=now)
                    await db.execute(
                        "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            namespace,
                            task_id,
                            "comment",
                            owner.owner_id,
                            self._json({"text": reason, "kind": "handoff"}),
                            now,
                        ),
                    )
                    await db.execute(
                        "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            namespace,
                            task_id,
                            "claim_released",
                            owner.owner_id,
                            self._json(
                                {
                                    "reason": reason,
                                    "release_reason": reason,
                                    "owner_kind": owner.kind,
                                    "owner_id": owner.owner_id,
                                }
                            ),
                            now,
                        ),
                    )
                committed = (
                    await self._committed_record_tx(db, namespace, task_id)
                    if capture_task
                    else None
                )
                await db.commit()
                if capture_task:
                    return committed
                return cur.rowcount > 0
            except Exception:
                await db.rollback()
                raise

    async def release_owner_claims_mutation(
        self,
        *,
        owner: ClaimOwner,
        reason: str,
        now: str | None = None,
    ) -> dict:
        """Release each observed claim atomically, preserving all workflow fields.

        Failed claims remain available for retry. Every successful release and its
        audit are committed together; exact claim ids fence late/repeated sweeps.
        This also handles historical owners with more than one pre-WIP claim.
        """
        now = now or utc_text()
        claims = await self.claims_for_owner(owner, active_only=True)
        released_count = 0
        errors = []
        for claim in claims:
            try:
                released = await self.release_owner_claim_mutation(
                    claim["namespace"],
                    claim["task_id"],
                    owner,
                    reason=reason,
                    now=now,
                    expected_claim_id=claim["id"],
                )
                released_count += int(released)
            except Exception as exc:
                errors.append(
                    {
                        "namespace": claim["namespace"],
                        "task_id": claim["task_id"],
                        "claim_id": claim["id"],
                        "code": "claim_release_failed",
                        "message": str(exc),
                    }
                )
        return {"ok": not errors, "released_count": released_count, "errors": errors}

    async def release_claims(
        self,
        *,
        agent_id: str | None = None,
        namespace: str | None = None,
        task_id: str | None = None,
        now: str | None = None,
    ) -> int:
        owner = ClaimOwner.legacy_session(agent_id) if agent_id is not None else None
        return await self.release_owner_claims(
            owner=owner, namespace=namespace, task_id=task_id, now=now
        )

    async def release_owner_claims(
        self,
        *,
        owner: ClaimOwner | None = None,
        namespace: str | None = None,
        task_id: str | None = None,
        now: str | None = None,
    ) -> int:
        where = ["released_at IS NULL"]
        params: list[Any] = []
        if owner is not None:
            where.extend(["owner_kind=?", "owner_id=?"])
            params.extend([owner.kind, owner.owner_id])
        for column, value in (("namespace", namespace), ("task_id", task_id)):
            if value is not None:
                where.append(f"{column}=?")
                params.append(value)
        now = now or utc_text()
        params.insert(0, now)
        async with self._connect() as db:
            cur = await db.execute(
                f"UPDATE work_claims SET released_at=? WHERE {' AND '.join(where)}", params
            )
            await db.commit()
        return cur.rowcount

    async def was_completed(self, namespace: str, task_id: str) -> bool:
        task = await self.get_task(namespace, task_id)
        if task is not None and task["state"] == "done":
            return True
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT payload_json FROM work_events "
                    "WHERE namespace=? AND task_id=? ORDER BY id",
                    (namespace, task_id),
                )
            ).fetchall()
        return any(self._loads(row[0], {}).get("state") == "done" for row in rows)

    async def _validate_dependency_graph_tx(self, db, namespace, task_id, normalized):
        source = (namespace, task_id)
        for dependency in normalized:
            if dependency == source:
                raise ValueError(f"self dependency is not allowed: {namespace}/{task_id}")
            queue = [dependency]
            seen = set()
            while queue:
                current = queue.pop(0)
                if current == source:
                    raise ValueError(f"dependency cycle detected for {namespace}/{task_id}")
                if current in seen:
                    continue
                seen.add(current)
                rows = await (
                    await db.execute(
                        "SELECT dependency_namespace,dependency_task_id FROM work_dependencies WHERE namespace=? AND task_id=?",
                        current,
                    )
                ).fetchall()
                queue.extend((row[0], row[1]) for row in rows)

    async def dependencies(self, namespace: str, task_id: str):
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT dependency_namespace,dependency_task_id,created_at FROM work_dependencies "
                    "WHERE namespace=? AND task_id=? ORDER BY dependency_namespace,dependency_task_id",
                    (namespace, task_id),
                )
            ).fetchall()
        return [{"namespace": row[0], "task_id": row[1], "created_at": row[2]} for row in rows]

    async def set_dependencies(
        self, namespace: str, task_id: str, dependencies, *, now: str | None = None
    ):
        now = now or utc_text()
        normalized = self._normalize_dependencies(namespace, dependencies)
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            exists = await (
                await db.execute(
                    "SELECT 1 FROM work_items WHERE namespace=? AND task_id=?", (namespace, task_id)
                )
            ).fetchone()
            if exists is None:
                await db.rollback()
                raise KeyError(f"unknown task: {namespace}/{task_id}")
            await self._validate_dependency_graph_tx(db, namespace, task_id, normalized)
            await db.execute(
                "DELETE FROM work_dependencies WHERE namespace=? AND task_id=?",
                (namespace, task_id),
            )
            await db.executemany(
                "INSERT INTO work_dependencies(namespace,task_id,dependency_namespace,dependency_task_id,created_at) "
                "VALUES(?,?,?,?,?)",
                [(namespace, task_id, dep_ns, dep_id, now) for dep_ns, dep_id in normalized],
            )
            await db.commit()
        return await self.dependencies(namespace, task_id)

    async def relations(self, namespace: str, task_id: str):
        async with self._connect() as db:
            outgoing = await (
                await db.execute(
                    "SELECT relation_kind,related_namespace,related_task_id,created_at,created_by FROM work_relations WHERE namespace=? AND task_id=? ORDER BY relation_kind,related_namespace,related_task_id",
                    (namespace, task_id),
                )
            ).fetchall()
            incoming = await (
                await db.execute(
                    "SELECT relation_kind,namespace,task_id,created_at,created_by FROM work_relations WHERE related_namespace=? AND related_task_id=? ORDER BY relation_kind,namespace,task_id",
                    (namespace, task_id),
                )
            ).fetchall()
        return [
            {
                "direction": "outgoing",
                "kind": r[0],
                "namespace": r[1],
                "task_id": r[2],
                "created_at": r[3],
                "created_by": r[4],
            }
            for r in outgoing
        ] + [
            {
                "direction": "incoming",
                "kind": r[0],
                "namespace": r[1],
                "task_id": r[2],
                "created_at": r[3],
                "created_by": r[4],
            }
            for r in incoming
        ]

    async def add_relation(
        self,
        namespace: str,
        task_id: str,
        *,
        related_namespace: str,
        related_task_id: str,
        relation_kind: str,
        agent_id: str,
        capture_task: bool = False,
        now: str | None = None,
        expected_revision: int | None = None,
        expected_claim_ids: tuple[int, ...] | None = None,
    ):
        now = now or utc_text()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_claim_snapshot(db, namespace, task_id, expected_claim_ids)
                await self._assert_task_revision(db, namespace, task_id, expected_revision)
                source = await (
                    await db.execute(
                        "SELECT lane,candidate_ref FROM work_items WHERE namespace=? AND task_id=?",
                        (namespace, task_id),
                    )
                ).fetchone()
                if source is None:
                    raise KeyError(f"unknown task: {namespace}/{task_id}")
                target = await (
                    await db.execute(
                        "SELECT candidate_ref FROM work_items WHERE namespace=? AND task_id=?",
                        (related_namespace, related_task_id),
                    )
                ).fetchone()
                if target is None:
                    raise KeyError(f"unknown task: {related_namespace}/{related_task_id}")

                bound_candidate = None
                candidate_changed = False
                if relation_kind == "review_of":
                    if source[0] != "review":
                        raise TaskRelationConflict(
                            "review_task_required",
                            "review_of source task must use lane=review",
                        )
                    parent_candidate = (target[0] or "").strip()
                    if not parent_candidate:
                        raise TaskRelationConflict(
                            "missing_parent_candidate",
                            "review_of parent task has no candidate_ref",
                        )
                    existing = await (
                        await db.execute(
                            "SELECT related_namespace,related_task_id FROM work_relations "
                            "WHERE namespace=? AND task_id=? AND relation_kind='review_of'",
                            (namespace, task_id),
                        )
                    ).fetchall()
                    if any(
                        row[0] != related_namespace or row[1] != related_task_id for row in existing
                    ):
                        raise TaskRelationConflict(
                            "ambiguous_review_parent",
                            "review task already has a different review_of parent",
                        )
                    review_candidate = (source[1] or "").strip()
                    if review_candidate and review_candidate != parent_candidate:
                        raise TaskRelationConflict(
                            "candidate_conflict",
                            "review task candidate_ref conflicts with parent candidate_ref",
                        )
                    bound_candidate = parent_candidate
                    if not review_candidate:
                        await db.execute(
                            "UPDATE work_items SET candidate_ref=?,revision=revision+1,updated_at=? "
                            "WHERE namespace=? AND task_id=?",
                            (parent_candidate, now, namespace, task_id),
                        )
                        candidate_changed = True

                cur = await db.execute(
                    "INSERT OR IGNORE INTO work_relations(namespace,task_id,related_namespace,related_task_id,relation_kind,created_at,created_by) VALUES(?,?,?,?,?,?,?)",
                    (
                        namespace,
                        task_id,
                        related_namespace,
                        related_task_id,
                        relation_kind,
                        now,
                        agent_id,
                    ),
                )
                if candidate_changed:
                    await db.execute(
                        "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            namespace,
                            task_id,
                            "candidate_bound",
                            agent_id,
                            self._json(
                                {
                                    "candidate_ref": bound_candidate,
                                    "parent_namespace": related_namespace,
                                    "parent_task_id": related_task_id,
                                }
                            ),
                            now,
                        ),
                    )
                if cur.rowcount:
                    payload = {
                        "kind": relation_kind,
                        "namespace": related_namespace,
                        "task_id": related_task_id,
                    }
                    if bound_candidate is not None:
                        payload["candidate_ref"] = bound_candidate
                    await db.execute(
                        "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            namespace,
                            task_id,
                            "relation_added",
                            agent_id,
                            self._json(payload),
                            now,
                        ),
                    )
                committed = (
                    await self._committed_record_tx(db, namespace, task_id)
                    if capture_task
                    else None
                )
                await db.commit()
                if capture_task:
                    return committed
                return cur.rowcount > 0
            except Exception:
                await db.rollback()
                raise

    async def remove_relation(
        self,
        namespace: str,
        task_id: str,
        *,
        related_namespace: str,
        related_task_id: str,
        relation_kind: str,
        agent_id: str,
        capture_task: bool = False,
        now: str | None = None,
        expected_revision: int | None = None,
        expected_claim_ids: tuple[int, ...] | None = None,
    ):
        now = now or utc_text()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_claim_snapshot(db, namespace, task_id, expected_claim_ids)
                await self._assert_task_revision(db, namespace, task_id, expected_revision)
                cur = await db.execute(
                    "DELETE FROM work_relations WHERE namespace=? AND task_id=? AND related_namespace=? AND related_task_id=? AND relation_kind=?",
                    (namespace, task_id, related_namespace, related_task_id, relation_kind),
                )
                if cur.rowcount:
                    await db.execute(
                        "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            namespace,
                            task_id,
                            "relation_removed",
                            agent_id,
                            self._json(
                                {
                                    "kind": relation_kind,
                                    "namespace": related_namespace,
                                    "task_id": related_task_id,
                                }
                            ),
                            now,
                        ),
                    )
                committed = (
                    await self._committed_record_tx(db, namespace, task_id)
                    if capture_task
                    else None
                )
                await db.commit()
                if capture_task:
                    return committed
                return cur.rowcount > 0
            except Exception:
                await db.rollback()
                raise

    async def output_states(self, namespace: str, task_id: str):
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT output_state_id,output_refs_json,created_at "
                    "FROM work_output_states WHERE namespace=? AND task_id=? "
                    "ORDER BY output_state_id",
                    (namespace, task_id),
                )
            ).fetchall()
        return [
            {
                "output_state_id": int(row[0]),
                "output_refs": self._loads(row[1], []),
                "created_at": row[2],
            }
            for row in rows
        ]

    async def reviews(
        self,
        namespace: str,
        task_id: str,
        *,
        output_state_id: int | None = None,
        candidate_ref: str | None = None,
    ):
        params: list[Any] = [namespace, task_id]
        clauses = []
        if output_state_id is not None:
            clauses.append("output_state_id=?")
            params.append(int(output_state_id))
        elif candidate_ref is not None:
            clauses.append("candidate_ref=?")
            params.append(candidate_ref)
        extra = (" AND " + " AND ".join(clauses)) if clauses else ""
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT output_state_id,output_refs_json,dimension,verdict,agent_id,"
                    "evidence_json,warnings_json,reviewed_at,candidate_ref "
                    "FROM work_reviews WHERE namespace=? AND task_id=?"
                    f"{extra} ORDER BY reviewed_at,dimension",
                    params,
                )
            ).fetchall()
        return [
            {
                "output_state_id": int(row[0]) if row[0] is not None else None,
                "output_refs": self._loads(row[1], []),
                "dimension": row[2],
                "verdict": row[3],
                "agent_id": row[4],
                "evidence": self._loads(row[5], {}),
                "warnings": self._loads(row[6], []),
                "reviewed_at": row[7],
                "candidate_ref": row[8] or None,
            }
            for row in rows
        ]

    async def upsert_reviews(
        self,
        namespace: str,
        task_id: str,
        *,
        output_state_id: int | None = None,
        output_refs=None,
        candidate_ref: str | None = None,
        dimensions,
        verdict: str,
        agent_id: str,
        evidence: Any = None,
        warnings: Any = None,
        capture_task: bool = False,
        event_payload: dict | None = None,
        now: str | None = None,
        expected_revision: int | None = None,
    ):
        dimensions = list(dict.fromkeys(dimensions or []))
        if not dimensions:
            raise ValueError("at least one review dimension is required")
        for dimension in dimensions:
            if dimension not in REVIEW_DIMENSIONS:
                raise ValueError(f"unsupported review dimension: {dimension}")
        now = now or utc_text()

        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_task_revision(db, namespace, task_id, expected_revision)
                current = await (
                    await db.execute(
                        "SELECT output_state_id,output_refs_json FROM work_items "
                        "WHERE namespace=? AND task_id=?",
                        (namespace, task_id),
                    )
                ).fetchone()
                if current is None:
                    raise KeyError(f"unknown task: {namespace}/{task_id}")
                current_state_id = current[0]
                current_output_refs = self._loads(current[1], [])
                if current_state_id is None:
                    raise ValueError("task has no output state")
                if output_state_id is None:
                    output_state_id = int(current_state_id)
                elif int(output_state_id) != int(current_state_id):
                    raise ValueError("task output state changed before review was recorded")
                if output_refs is None:
                    output_refs = current_output_refs
                else:
                    output_refs = self._normalize_refs(output_refs)
                    if output_refs != current_output_refs:
                        raise ValueError("task output refs changed before review was recorded")
                stored_candidate = candidate_ref or f"@output-state:{int(output_state_id)}"
                await db.executemany(
                    "INSERT INTO work_reviews(namespace,task_id,candidate_ref,dimension,verdict,"
                    "agent_id,evidence_json,warnings_json,reviewed_at,output_state_id,output_refs_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(namespace,task_id,output_state_id,dimension) DO UPDATE SET "
                    "verdict=excluded.verdict,agent_id=excluded.agent_id,"
                    "evidence_json=excluded.evidence_json,warnings_json=excluded.warnings_json,"
                    "reviewed_at=excluded.reviewed_at,output_refs_json=excluded.output_refs_json,"
                    "candidate_ref=excluded.candidate_ref",
                    [
                        (
                            namespace,
                            task_id,
                            stored_candidate,
                            dimension,
                            verdict,
                            agent_id,
                            self._json(evidence or {}),
                            self._json(warnings or []),
                            now,
                            int(output_state_id),
                            self._json(output_refs),
                        )
                        for dimension in dimensions
                    ],
                )
                if event_payload is not None:
                    await db.execute(
                        "INSERT INTO work_events(namespace,task_id,event_type,agent_id,"
                        "payload_json,created_at) VALUES(?,?,?,?,?,?)",
                        (namespace, task_id, "review", agent_id, self._json(event_payload), now),
                    )
                committed = (
                    await self._committed_record_tx(db, namespace, task_id)
                    if capture_task
                    else None
                )
                await db.commit()
                if capture_task:
                    return committed
            except Exception:
                await db.rollback()
                raise
        rows = await self.reviews(namespace, task_id, output_state_id=int(output_state_id))
        by_dimension = {row["dimension"]: row for row in rows}
        return [by_dimension[dimension] for dimension in dimensions]

    async def upsert_review(
        self,
        namespace: str,
        task_id: str,
        *,
        output_state_id: int | None = None,
        output_refs=None,
        candidate_ref: str | None = None,
        dimension: str,
        verdict: str,
        agent_id: str,
        evidence: Any = None,
        warnings: Any = None,
        now: str | None = None,
    ):
        rows = await self.upsert_reviews(
            namespace,
            task_id,
            output_state_id=output_state_id,
            output_refs=output_refs,
            candidate_ref=candidate_ref,
            dimensions=[dimension],
            verdict=verdict,
            agent_id=agent_id,
            evidence=evidence,
            warnings=warnings,
            now=now,
        )
        return rows[0]

    async def persistent_task_commands(
        self, namespace: str, task_id: str, logical_agent_id: str
    ) -> list[dict]:
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT json_extract(payload_json,'$.command_hash'),work_session_id,session_epoch "
                    "FROM work_events WHERE namespace=? AND task_id=? AND event_type='command' "
                    "AND logical_agent_id=? AND json_extract(payload_json,'$.command_hash') IS NOT NULL "
                    "ORDER BY id DESC",
                    (namespace, task_id, logical_agent_id),
                )
            ).fetchall()
        seen = set()
        result = []
        for command_hash, work_session_id, session_epoch in rows:
            if command_hash in seen:
                continue
            seen.add(command_hash)
            result.append(
                {
                    "command_hash": command_hash,
                    "work_session_id": work_session_id,
                    "session_epoch": session_epoch,
                }
            )
        return result

    async def add_event(
        self,
        namespace: str,
        task_id: str,
        event_type: str,
        *,
        agent_id: str | None = None,
        payload: Any = None,
        capture_task: bool = False,
        expected_revision: int | None = None,
        now: str | None = None,
        logical_agent_id: str | None = None,
        work_session_id: str | None = None,
        session_epoch: int | None = None,
    ):
        now = now or utc_text()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._assert_task_revision(db, namespace, task_id, expected_revision)
            cur = await db.execute(
                "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at,"
                "logical_agent_id,work_session_id,session_epoch) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    namespace,
                    task_id,
                    event_type,
                    agent_id,
                    self._json(payload or {}),
                    now,
                    logical_agent_id,
                    work_session_id,
                    session_epoch,
                ),
            )
            committed = (
                await self._committed_record_tx(db, namespace, task_id) if capture_task else None
            )
            await db.commit()
        if capture_task:
            return committed
        result = {
            "id": cur.lastrowid,
            "event_type": event_type,
            "agent_id": agent_id,
            "payload": payload or {},
            "created_at": now,
        }
        if logical_agent_id is not None or work_session_id is not None or session_epoch is not None:
            result.update(
                {
                    "logical_agent_id": logical_agent_id,
                    "work_session_id": work_session_id,
                    "session_epoch": session_epoch,
                }
            )
        return result

    async def latest_checkpoint_event(self, namespace: str, task_id: str):
        async with self._connect() as db:
            row = await (
                await db.execute(
                    "SELECT id,event_type,agent_id,payload_json,created_at FROM work_events "
                    "WHERE namespace=? AND task_id=? "
                    "AND json_type(payload_json, '$.checkpoint') IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1",
                    (namespace, task_id),
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "event_type": row[1],
            "agent_id": row[2],
            "payload": self._loads(row[3], {}),
            "created_at": row[4],
        }

    async def list_events(
        self,
        namespace: str,
        task_id: str,
        *,
        limit: int = 100,
        before_id: int | None = None,
    ):
        params: list[Any] = [namespace, task_id]
        clause = ""
        if before_id is not None:
            clause = " AND id<?"
            params.append(int(before_id))
        params.append(max(1, min(int(limit), 1000)))
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT id,event_type,agent_id,payload_json,created_at,logical_agent_id,"
                    "work_session_id,session_epoch FROM work_events "
                    "WHERE namespace=? AND task_id=?"
                    f"{clause} ORDER BY id DESC LIMIT ?",
                    params,
                )
            ).fetchall()
        result = []
        for row in rows:
            event = {
                "id": row[0],
                "event_type": row[1],
                "agent_id": row[2],
                "payload": self._loads(row[3], {}),
                "created_at": row[4],
            }
            if row[5] is not None or row[6] is not None or row[7] is not None:
                event.update(
                    {
                        "logical_agent_id": row[5],
                        "work_session_id": row[6],
                        "session_epoch": row[7],
                    }
                )
            result.append(event)
        return result
