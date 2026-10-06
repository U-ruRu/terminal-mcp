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
        return {
            "id": int(row[0]),
            "summary": row[1],
            "content": row[2],
            "primary": bool(row[3]),
        }

    async def list(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        primary_first: bool = False,
    ):
        order = "is_primary DESC,id ASC" if primary_first else "id ASC"
        query = (
            "SELECT id,summary,content,is_primary FROM instance_context "
            f"ORDER BY {order}"
        )
        params: list[int] = []
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend((max(1, int(limit)), max(0, int(offset))))
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            rows = await (await db.execute(query, params)).fetchall()
        return [self._entry(row) for row in rows]

    async def get(self, context_id: int):
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            row = await (
                await db.execute(
                    "SELECT id,summary,content,is_primary FROM instance_context WHERE id=?",
                    (int(context_id),),
                )
            ).fetchone()
        return self._entry(row)

    async def create(self, summary: str, content: str, primary: bool):
        summary = self._summary(summary)
        content = self._content(content)
        primary = self._primary(primary)
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            cur = await db.execute(
                "INSERT INTO instance_context(summary,content,is_primary) VALUES(?,?,?)",
                (summary, content, int(primary)),
            )
            await db.commit()
            context_id = int(cur.lastrowid)
        return await self.get(context_id)

    async def update(
        self,
        context_id: int,
        *,
        summary: Any = _UNSET,
        content: Any = _UNSET,
        primary: Any = _UNSET,
    ):
        assignments = []
        params = []
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
        params.append(int(context_id))
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            cur = await db.execute(
                f"UPDATE instance_context SET {','.join(assignments)} WHERE id=?",
                params,
            )
            await db.commit()
            if cur.rowcount != 1:
                return None
        return await self.get(context_id)

    async def delete(self, context_id: int) -> bool:
        async with cancellation_safe_connection(aiosqlite.connect, self.path, timeout=1.0) as db:
            cur = await db.execute("DELETE FROM instance_context WHERE id=?", (int(context_id),))
            await db.commit()
        return cur.rowcount == 1
