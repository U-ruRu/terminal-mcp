# ruff: noqa: E501
from __future__ import annotations

import json

import aiosqlite


class AgentStore:
    def __init__(self, path):
        self.path = path

    async def create_session(self, agent_id, task_summary, intent, work_scope, now):
        scope = json.dumps(work_scope, separators=(",", ":"))
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            await db.execute(
                "INSERT INTO agent_sessions VALUES(?,?,?,?,?,?,?)",
                (agent_id, now, now, task_summary, intent, scope, "active"),
            )
            await db.execute(
                "INSERT INTO agent_task_events(agent_id,timestamp,intent,work_scope) VALUES(?,?,?,?)",
                (agent_id, now, intent, scope),
            )
            await db.commit()

    async def get_session(self, agent_id):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            row = await (
                await db.execute(
                    "SELECT agent_id,registered_at,last_activity_at,task_summary,intent,work_scope,state "
                    "FROM agent_sessions WHERE agent_id=?",
                    (agent_id,),
                )
            ).fetchone()
        return self._session(row) if row else None

    async def touch(self, agent_id, now):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            cur = await db.execute(
                "UPDATE agent_sessions SET last_activity_at=?,state='active' WHERE agent_id=?",
                (now, agent_id),
            )
            await db.commit()
            return cur.rowcount == 1

    async def expire(self, agent_id):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            await db.execute(
                "UPDATE agent_sessions SET state='expired' WHERE agent_id=?", (agent_id,)
            )
            await db.commit()

    async def finish(self, agent_id):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            await db.execute(
                "UPDATE agent_sessions SET state='finished' WHERE agent_id=?", (agent_id,)
            )
            await db.commit()

    async def update_task(self, agent_id, intent, work_scope, now):
        scope = json.dumps(work_scope, separators=(",", ":"))
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            await db.execute(
                "UPDATE agent_sessions SET last_activity_at=?,intent=?,work_scope=?,state='active' WHERE agent_id=?",
                (now, intent, scope, agent_id),
            )
            await db.execute(
                "INSERT INTO agent_task_events(agent_id,timestamp,intent,work_scope) VALUES(?,?,?,?)",
                (agent_id, now, intent, scope),
            )
            await db.commit()

    async def activity(self, agent_id, tool, now, command_hash=None):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            await db.execute(
                "INSERT INTO agent_activity_events(agent_id,timestamp,tool,command_hash) VALUES(?,?,?,?)",
                (agent_id, now, tool, command_hash),
            )
            await db.commit()

    async def active(self, cutoff, limit):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (
                await db.execute(
                    "SELECT agent_id,registered_at,last_activity_at,task_summary,intent,work_scope,state "
                    "FROM agent_sessions WHERE state='active' AND last_activity_at>=? "
                    "ORDER BY last_activity_at DESC LIMIT ?",
                    (cutoff, limit),
                )
            ).fetchall()
        return [self._session(row) for row in rows]

    async def count_active(self, cutoff):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            row = await (
                await db.execute(
                    "SELECT COUNT(*) FROM agent_sessions WHERE state='active' AND last_activity_at>=?",
                    (cutoff,),
                )
            ).fetchone()
        return int(row[0])

    async def recent_commands(self, agent_id, limit=3):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (
                await db.execute(
                    "SELECT a.created_at,a.command_hash,a.command_preview,c.status,a.command_type "
                    "FROM command_agent_attribution a JOIN commands c ON c.hash=a.command_hash "
                    "WHERE a.agent_id=? ORDER BY a.created_at DESC, a.rowid DESC LIMIT ?",
                    (agent_id, limit),
                )
            ).fetchall()
        return [
            {
                "created_at": r[0],
                "command_hash": r[1],
                "preview": r[2],
                "status": r[3],
                "command_type": r[4],
            }
            for r in rows
        ]

    async def command_agents(self, hashes):
        values = list(dict.fromkeys(hashes))
        if not values:
            return {}
        marks = ",".join("?" for _ in values)
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (
                await db.execute(
                    f"SELECT command_hash,agent_id FROM command_agent_attribution WHERE command_hash IN ({marks})",
                    values,
                )
            ).fetchall()
        return dict(rows)

    @staticmethod
    def _session(row):
        return {
            "agent_id": row[0],
            "registered_at": row[1],
            "last_activity_at": row[2],
            "task_summary": row[3],
            "intent": row[4],
            "work_scope": json.loads(row[5]),
            "state": row[6],
        }
