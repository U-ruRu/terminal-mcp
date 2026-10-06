from __future__ import annotations

from typing import Any

import aiosqlite

from terminal_mcp.storage.sqlite_observability import cancellation_safe_connection

_UNSET = object()


class ContextStore:
    def __init__(self, path):
        self.path = path

    @staticmethod
    def _summary(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("summary must be a string")
        value = value.strip()
        if not value:
            raise ValueError("summary is required")
        if len(value) > 100:
            raise ValueError("summary must be at most 100 characters")
        return value

    @staticmethod
    def _content(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("content must be a string")
        value = value.strip()
        if not value:
            raise ValueError("content is required")
        return value

    @staticmethod
    def _primary(value: bool) -> bool:
        if not isinstance(value, bool):
            raise ValueError("primary must be a boolean")
        return value

    @staticmethod
    def _entry(row):
        if row is None:
            return None
        item = {
            "id": int(row[0]),
            "summary": row[1],
            "content": row[2],
            "primary": bool(row[3]),
        }
        if len(row) > 4 and row[4] is not None:
            item["namespace"] = row[4]
        return item

    async def list(
        self,
        *,
        namespace: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        primary_first: bool = False,
    ):
        order = "is_primary DESC,id ASC" if primary_first else "id ASC"
        if namespace is None:
            query = (
                "SELECT id,summary,content,is_primary,namespace FROM instance_context "
                "WHERE namespace IS NULL "
                f"ORDER BY {order}"
            )
            params: list[Any] = []
        else:
            query = (
                "SELECT id,summary,content,is_primary,namespace FROM instance_context "
                "WHERE namespace=? "
                f"ORDER BY {order}"
            )
            params = [namespace]
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend((max(1, int(limit)), max(0, int(offset))))
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            rows = await (await db.execute(query, params)).fetchall()
        return [self._entry(row) for row in rows]

    async def get(self, context_id: int, *, namespace: Any = _UNSET):
        query = "SELECT id,summary,content,is_primary,namespace FROM instance_context WHERE id=?"
        params: list[Any] = [int(context_id)]
        if namespace is not _UNSET:
            if namespace is None:
                query += " AND namespace IS NULL"
            else:
                query += " AND namespace=?"
                params.append(namespace)
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            row = await (await db.execute(query, params)).fetchone()
        return self._entry(row)

    async def create(
        self, summary: str, content: str, primary: bool, *, namespace: str | None = None
    ):
        summary = self._summary(summary)
        content = self._content(content)
        primary = self._primary(primary)
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            cur = await db.execute(
                "INSERT INTO instance_context(summary,content,is_primary,namespace) "
                "VALUES(?,?,?,?)",
                (summary, content, int(primary), namespace),
            )
            await db.commit()
            context_id = int(cur.lastrowid)
        return await self.get(context_id, namespace=namespace)

    async def update(
        self,
        context_id: int,
        *,
        namespace: Any = _UNSET,
        summary: Any = _UNSET,
        content: Any = _UNSET,
        primary: Any = _UNSET,
    ):
        assignments = []
        params: list[Any] = []
        if summary is not _UNSET:
            assignments.append("summary=?")
            params.append(self._summary(summary))
        if content is not _UNSET:
            assignments.append("content=?")
            params.append(self._content(content))
        if primary is not _UNSET:
            assignments.append("is_primary=?")
            params.append(int(self._primary(primary)))
        if not assignments:
            raise ValueError("at least one context field is required")
        where = "id=?"
        params.append(int(context_id))
        if namespace is not _UNSET:
            if namespace is None:
                where += " AND namespace IS NULL"
            else:
                where += " AND namespace=?"
                params.append(namespace)
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            cur = await db.execute(
                f"UPDATE instance_context SET {','.join(assignments)} WHERE {where}", params
            )
            await db.commit()
            if cur.rowcount != 1:
                return None
        return await self.get(context_id, namespace=namespace)

    async def delete(self, context_id: int, *, namespace: Any = _UNSET) -> bool:
        query = "DELETE FROM instance_context WHERE id=?"
        params: list[Any] = [int(context_id)]
        if namespace is not _UNSET:
            if namespace is None:
                query += " AND namespace IS NULL"
            else:
                query += " AND namespace=?"
                params.append(namespace)
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            cur = await db.execute(query, params)
            await db.commit()
        return cur.rowcount == 1

    async def mark_namespace_seen(
        self, work_session_id: str, namespace: str, *, seen_at: str
    ) -> None:
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            await db.execute(
                "INSERT INTO work_session_namespace_context_seen"
                "(work_session_id,namespace,seen_at) "
                "VALUES(?,?,?) ON CONFLICT(work_session_id,namespace) "
                "DO UPDATE SET seen_at=excluded.seen_at",
                (work_session_id, namespace, seen_at),
            )
            await db.commit()

    async def namespace_seen(self, work_session_id: str, namespace: str) -> bool:
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            row = await (
                await db.execute(
                    "SELECT 1 FROM work_session_namespace_context_seen "
                    "WHERE work_session_id=? AND namespace=?",
                    (work_session_id, namespace),
                )
            ).fetchone()
        return row is not None
