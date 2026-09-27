# ruff: noqa: E501
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

from terminal_mcp.core.orchestration import utc_text

LANES = frozenset({"implementation", "review", "release", "integration", "general"})
STATES = frozenset({"ready", "blocked", "deferred", "done"})
REVIEW_DIMENSIONS = frozenset({"A", "C", "R"})


class TaskRevisionConflict(RuntimeError):
    def __init__(self, namespace: str, task_id: str, expected: int, actual: int):
        self.namespace = namespace
        self.task_id = task_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"task revision conflict for {namespace}/{task_id}: expected {expected}, actual {actual}"
        )


class TaskStore:
    def __init__(self, path):
        self.path = path

    @asynccontextmanager
    async def _connect(self):
        db = await aiosqlite.connect(self.path, timeout=1.0)
        await db.execute("PRAGMA busy_timeout=1000")
        await db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            await db.close()

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
            "revision": row[13],
            "created_at": row[14],
            "updated_at": row[15],
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
        resource: Any = None,
        reviews: Any = None,
        cooperative: bool = False,
        checkpoint: Any = None,
        candidate_ref: str | None = None,
        now: str | None = None,
    ):
        if not namespace or not task_id or not title:
            raise ValueError("namespace, task_id and title are required")
        self._validate_lane(lane)
        self._validate_state(state)
        now = now or utc_text()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO work_items(namespace,task_id,title,lane,priority,state,description,next_action,"
                "resource_json,reviews_json,cooperative,checkpoint_json,candidate_ref,revision,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                (
                    namespace,
                    task_id,
                    title,
                    lane,
                    int(priority),
                    state,
                    description,
                    next_action,
                    self._json(resource or {}),
                    self._json(reviews or []),
                    int(bool(cooperative)),
                    self._json(checkpoint or {}),
                    candidate_ref,
                    now,
                    now,
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
        resource: Any = None,
        reviews: Any = None,
        cooperative: bool = False,
        checkpoint: Any = None,
        candidate_ref: str | None = None,
        dependencies=None,
        event_agent_id: str | None = None,
        event_payload: Any = None,
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
                await db.execute(
                    "INSERT INTO work_items(namespace,task_id,title,lane,priority,state,description,next_action,"
                    "resource_json,reviews_json,cooperative,checkpoint_json,candidate_ref,revision,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                    (
                        namespace,
                        task_id,
                        title,
                        lane,
                        int(priority),
                        state,
                        description,
                        next_action,
                        self._json(resource or {}),
                        self._json(reviews or []),
                        int(bool(cooperative)),
                        self._json(checkpoint or {}),
                        candidate_ref,
                        now,
                        now,
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
                        self._json(event_payload or {}),
                        now,
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.get_task(namespace, task_id)

    async def get_task(self, namespace: str, task_id: str):
        async with self._connect() as db:
            row = await (
                await db.execute(
                    "SELECT namespace,task_id,title,lane,priority,state,description,next_action,resource_json,"
                    "reviews_json,cooperative,checkpoint_json,candidate_ref,revision,created_at,updated_at "
                    "FROM work_items WHERE namespace=? AND task_id=?",
                    (namespace, task_id),
                )
            ).fetchone()
        return self._task(row)

    async def list_tasks(
        self,
        *,
        namespace: str | None = None,
        lane: str | None = None,
        state: str | None = None,
        show_done: bool = False,
        limit: int = 100,
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
        elif not show_done:
            where.append("state<>'done'")
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        params.extend([max(1, min(int(limit), 1000)), max(0, int(offset))])
        query = (
            "SELECT namespace,task_id,title,lane,priority,state,description,next_action,resource_json,"
            "reviews_json,cooperative,checkpoint_json,candidate_ref,revision,created_at,updated_at "
            f"FROM work_items{clause} ORDER BY priority DESC,updated_at DESC,namespace,task_id LIMIT ? OFFSET ?"
        )
        async with self._connect() as db:
            rows = await (await db.execute(query, params)).fetchall()
        return [self._task(row) for row in rows]

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
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported task fields: {sorted(unknown)}")
        if not changes:
            return await self.get_task(namespace, task_id)
        if "lane" in changes:
            self._validate_lane(changes["lane"])
        if "state" in changes:
            self._validate_state(changes["state"])
        columns = {
            "resource": "resource_json",
            "reviews": "reviews_json",
            "checkpoint": "checkpoint_json",
        }
        assignments = []
        params: list[Any] = []
        for key, value in changes.items():
            column = columns.get(key, key)
            if key in {"resource", "reviews", "checkpoint"}:
                value = self._json(value if value is not None else ({} if key != "reviews" else []))
            elif key == "cooperative":
                value = int(bool(value))
            elif key == "priority":
                value = int(value)
            assignments.append(f"{column}=?")
            params.append(value)
        now = now or utc_text()
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
                        "SELECT revision FROM work_items WHERE namespace=? AND task_id=?",
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

    async def update_task_mutation(
        self,
        namespace: str,
        task_id: str,
        *,
        expected_revision: int | None = None,
        dependencies=None,
        event_type: str = "updated",
        event_agent_id: str | None = None,
        event_payload: Any = None,
        release_claims_reason: str | None = None,
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
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported task fields: {sorted(unknown)}")
        if "lane" in changes:
            self._validate_lane(changes["lane"])
        if "state" in changes:
            self._validate_state(changes["state"])
        normalized = self._normalize_dependencies(namespace, dependencies)
        columns = {
            "resource": "resource_json",
            "reviews": "reviews_json",
            "checkpoint": "checkpoint_json",
        }
        assignments = []
        params: list[Any] = []
        for key, value in changes.items():
            column = columns.get(key, key)
            if key in {"resource", "reviews", "checkpoint"}:
                value = self._json(value if value is not None else ({} if key != "reviews" else []))
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
                exists = await (
                    await db.execute(
                        "SELECT revision FROM work_items WHERE namespace=? AND task_id=?",
                        (namespace, task_id),
                    )
                ).fetchone()
                if exists is None:
                    raise KeyError(f"unknown task: {namespace}/{task_id}")
                if assignments:
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
                await db.execute(
                    "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        namespace,
                        task_id,
                        event_type,
                        event_agent_id,
                        self._json(event_payload or {}),
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
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.get_task(namespace, task_id)

    async def claims(self, namespace: str, task_id: str, *, active_only: bool = True):
        clause = " AND released_at IS NULL" if active_only else ""
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT id,agent_id,claimed_at,released_at FROM work_claims "
                    "WHERE namespace=? AND task_id=?" + clause + " ORDER BY claimed_at,id",
                    (namespace, task_id),
                )
            ).fetchall()
        return [
            {"id": r[0], "agent_id": r[1], "claimed_at": r[2], "released_at": r[3]} for r in rows
        ]

    async def claims_for_agent(self, agent_id: str, *, active_only: bool = True):
        clause = " AND c.released_at IS NULL" if active_only else ""
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT c.namespace,c.task_id,c.claimed_at,c.released_at,w.lane,w.priority,w.state "
                    "FROM work_claims c JOIN work_items w ON w.namespace=c.namespace AND w.task_id=c.task_id "
                    "WHERE c.agent_id=?" + clause + " ORDER BY c.claimed_at,c.id",
                    (agent_id,),
                )
            ).fetchall()
        return [
            {
                "namespace": r[0],
                "task_id": r[1],
                "claimed_at": r[2],
                "released_at": r[3],
                "lane": r[4],
                "priority": r[5],
                "state": r[6],
            }
            for r in rows
        ]

    async def all_active_claims(self):
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT namespace,task_id,agent_id,claimed_at FROM work_claims "
                    "WHERE released_at IS NULL ORDER BY claimed_at,id"
                )
            ).fetchall()
        return [
            {
                "namespace": row[0],
                "task_id": row[1],
                "agent_id": row[2],
                "claimed_at": row[3],
            }
            for row in rows
        ]

    async def stats(self):
        async with self._connect() as db:
            states = await (
                await db.execute("SELECT state,COUNT(*) FROM work_items GROUP BY state")
            ).fetchall()
            lanes = await (
                await db.execute(
                    "SELECT lane,COUNT(*) FROM work_items WHERE state<>'done' GROUP BY lane"
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
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT id,agent_id,claimed_at FROM work_claims "
                    "WHERE namespace=? AND task_id=? AND released_at IS NULL ORDER BY claimed_at,id",
                    (namespace, task_id),
                )
            ).fetchall()
        return [{"id": row[0], "agent_id": row[1], "claimed_at": row[2]} for row in rows]

    async def claim(self, namespace: str, task_id: str, agent_id: str, *, now: str | None = None):
        now = now or utc_text()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            existing = await (
                await db.execute(
                    "SELECT id,claimed_at FROM work_claims WHERE namespace=? AND task_id=? "
                    "AND agent_id=? AND released_at IS NULL",
                    (namespace, task_id, agent_id),
                )
            ).fetchone()
            if existing:
                await db.rollback()
                return {
                    "id": existing[0],
                    "agent_id": agent_id,
                    "claimed_at": existing[1],
                    "created": False,
                }
            task = await (
                await db.execute(
                    "SELECT 1 FROM work_items WHERE namespace=? AND task_id=?", (namespace, task_id)
                )
            ).fetchone()
            if task is None:
                await db.rollback()
                raise KeyError(f"unknown task: {namespace}/{task_id}")
            cur = await db.execute(
                "INSERT INTO work_claims(namespace,task_id,agent_id,claimed_at,released_at) VALUES(?,?,?,?,NULL)",
                (namespace, task_id, agent_id, now),
            )
            await db.commit()
            return {"id": cur.lastrowid, "agent_id": agent_id, "claimed_at": now, "created": True}

    async def release_claim(
        self, namespace: str, task_id: str, agent_id: str, *, now: str | None = None
    ) -> bool:
        now = now or utc_text()
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE work_claims SET released_at=? WHERE namespace=? AND task_id=? "
                "AND agent_id=? AND released_at IS NULL",
                (now, namespace, task_id, agent_id),
            )
            await db.commit()
        return cur.rowcount > 0

    async def release_claims(
        self,
        *,
        agent_id: str | None = None,
        namespace: str | None = None,
        task_id: str | None = None,
        now: str | None = None,
    ) -> int:
        where = ["released_at IS NULL"]
        params: list[Any] = []
        for column, value in (
            ("agent_id", agent_id),
            ("namespace", namespace),
            ("task_id", task_id),
        ):
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

    async def reviews(self, namespace: str, task_id: str, *, candidate_ref: str | None = None):
        params: list[Any] = [namespace, task_id]
        candidate_clause = ""
        if candidate_ref is not None:
            candidate_clause = " AND candidate_ref=?"
            params.append(candidate_ref)
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT candidate_ref,dimension,verdict,agent_id,evidence_json,warnings_json,reviewed_at "
                    "FROM work_reviews WHERE namespace=? AND task_id=?"
                    f"{candidate_clause} ORDER BY reviewed_at,dimension",
                    params,
                )
            ).fetchall()
        return [
            {
                "candidate_ref": row[0] or None,
                "dimension": row[1],
                "verdict": row[2],
                "agent_id": row[3],
                "evidence": self._loads(row[4], {}),
                "warnings": self._loads(row[5], []),
                "reviewed_at": row[6],
            }
            for row in rows
        ]

    async def upsert_review(
        self,
        namespace: str,
        task_id: str,
        *,
        candidate_ref: str | None,
        dimension: str,
        verdict: str,
        agent_id: str,
        evidence: Any = None,
        warnings: Any = None,
        now: str | None = None,
    ):
        if dimension not in REVIEW_DIMENSIONS:
            raise ValueError(f"unsupported review dimension: {dimension}")
        now = now or utc_text()
        stored_candidate = candidate_ref or ""
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO work_reviews(namespace,task_id,candidate_ref,dimension,verdict,agent_id,evidence_json,warnings_json,reviewed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(namespace,task_id,candidate_ref,dimension) DO UPDATE SET "
                "verdict=excluded.verdict,agent_id=excluded.agent_id,evidence_json=excluded.evidence_json,"
                "warnings_json=excluded.warnings_json,reviewed_at=excluded.reviewed_at",
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
                ),
            )
            await db.commit()
        rows = await self.reviews(namespace, task_id, candidate_ref=stored_candidate)
        return next(row for row in rows if row["dimension"] == dimension)

    async def add_event(
        self,
        namespace: str,
        task_id: str,
        event_type: str,
        *,
        agent_id: str | None = None,
        payload: Any = None,
        now: str | None = None,
    ):
        now = now or utc_text()
        async with self._connect() as db:
            cur = await db.execute(
                "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (namespace, task_id, event_type, agent_id, self._json(payload or {}), now),
            )
            await db.commit()
        return {
            "id": cur.lastrowid,
            "event_type": event_type,
            "agent_id": agent_id,
            "payload": payload or {},
            "created_at": now,
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
                    "SELECT id,event_type,agent_id,payload_json,created_at FROM work_events "
                    "WHERE namespace=? AND task_id=?"
                    f"{clause} ORDER BY id DESC LIMIT ?",
                    params,
                )
            ).fetchall()
        return [
            {
                "id": row[0],
                "event_type": row[1],
                "agent_id": row[2],
                "payload": self._loads(row[3], {}),
                "created_at": row[4],
            }
            for row in rows
        ]
