# ruff: noqa: E501
from __future__ import annotations

import json

import aiosqlite

_SESSION_COLUMNS = (
    "agent_id,registered_at,last_activity_at,task_summary,intent,work_scope,state,"
    "details,current_step"
)


class AgentStore:
    def __init__(self, path):
        self.path = path

    async def create_session(
        self, agent_id, task_summary, intent, work_scope, details, current_step, now
    ):
        scope = json.dumps(work_scope or [], separators=(",", ":"))
        plan = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            await db.execute(
                "INSERT INTO agent_sessions("
                "agent_id,registered_at,last_activity_at,task_summary,intent,work_scope,state,"
                "details,current_step) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    agent_id,
                    now,
                    now,
                    task_summary,
                    intent,
                    scope,
                    "active",
                    plan,
                    current_step,
                ),
            )
            await db.execute(
                "INSERT INTO agent_task_events(agent_id,timestamp,intent,work_scope,step) "
                "VALUES(?,?,?,?,?)",
                (agent_id, now, intent, scope, current_step),
            )
            await db.commit()

    async def update_session(
        self, agent_id, task_summary, intent, work_scope, details, current_step, now
    ):
        scope = json.dumps(work_scope or [], separators=(",", ":"))
        plan = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            cur = await db.execute(
                "UPDATE agent_sessions SET last_activity_at=?,task_summary=?,intent=?,"
                "work_scope=?,details=?,current_step=?,state='active' "
                "WHERE agent_id=? AND state='active'",
                (now, task_summary, intent, scope, plan, current_step, agent_id),
            )
            if cur.rowcount:
                await db.execute(
                    "INSERT INTO agent_task_events(agent_id,timestamp,intent,work_scope,step) "
                    "VALUES(?,?,?,?,?)",
                    (agent_id, now, intent, scope, current_step),
                )
            await db.commit()
            return cur.rowcount == 1

    async def get_session(self, agent_id):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            row = await (
                await db.execute(
                    f"SELECT {_SESSION_COLUMNS} FROM agent_sessions WHERE agent_id=?",
                    (agent_id,),
                )
            ).fetchone()
        return self._session(row) if row else None

    async def touch(self, agent_id, now):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            cur = await db.execute(
                "UPDATE agent_sessions SET last_activity_at=? WHERE agent_id=? AND state='active'",
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

    async def update_coordinate(self, agent_id, intent, step, now):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            row = await (
                await db.execute(
                    "SELECT work_scope FROM agent_sessions WHERE agent_id=? AND state='active'",
                    (agent_id,),
                )
            ).fetchone()
            if row is None:
                return False
            await db.execute(
                "UPDATE agent_sessions SET intent=?,current_step=? WHERE agent_id=? AND state='active'",
                (intent, step, agent_id),
            )
            await db.execute(
                "INSERT INTO agent_task_events(agent_id,timestamp,intent,work_scope,step) "
                "VALUES(?,?,?,?,?)",
                (agent_id, now, intent, row[0], step),
            )
            await db.commit()
            return True

    async def activity(self, agent_id, tool, now, command_hash=None):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            await db.execute(
                "INSERT INTO agent_activity_events(agent_id,timestamp,tool,command_hash) VALUES(?,?,?,?)",
                (agent_id, now, tool, command_hash),
            )
            await db.commit()

    async def latest_task_at(self, agent_id):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            row = await (
                await db.execute(
                    "SELECT timestamp FROM agent_task_events "
                    "WHERE agent_id=? ORDER BY id DESC LIMIT 1",
                    (agent_id,),
                )
            ).fetchone()
        return row[0] if row else None

    async def active(self, cutoff, limit):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (
                await db.execute(
                    f"SELECT {_SESSION_COLUMNS} FROM agent_sessions "
                    "WHERE state='active' AND last_activity_at>=? "
                    "ORDER BY last_activity_at DESC LIMIT ?",
                    (cutoff, limit),
                )
            ).fetchall()
        return [self._session(row) for row in rows]

    async def recent_sessions(self, cutoff):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (
                await db.execute(
                    f"SELECT {_SESSION_COLUMNS} FROM agent_sessions WHERE last_activity_at>=? "
                    "ORDER BY last_activity_at DESC",
                    (cutoff,),
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

    async def active_with_latest_command(self, cutoff, exclude_agent_id=None, limit=8):
        params = [cutoff]
        exclusion = ""
        if exclude_agent_id:
            exclusion = "AND s.agent_id<>? "
            params.append(exclude_agent_id)
        params.append(limit)
        query = (
            "SELECT s.agent_id,s.intent,s.registered_at,a.command_hash,c.started_at,c.finished_at "
            "FROM agent_sessions s "
            "LEFT JOIN command_agent_attribution a ON a.rowid=("
            "SELECT a2.rowid FROM command_agent_attribution a2 "
            "WHERE a2.agent_id=s.agent_id ORDER BY a2.created_at DESC,a2.rowid DESC LIMIT 1"
            ") "
            "LEFT JOIN commands c ON c.hash=a.command_hash "
            "WHERE s.state='active' AND s.last_activity_at>=? "
            f"{exclusion}"
            "ORDER BY s.last_activity_at DESC LIMIT ?"
        )
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (await db.execute(query, params)).fetchall()
        return [
            {
                "agent_id": row[0],
                "intent": row[1],
                "registered_at": row[2],
                "last_command_hash": row[3],
                "last_command_started_at": row[4],
                "last_command_finished_at": row[5],
            }
            for row in rows
        ]

    async def recent_lifecycle(self, cutoff, exclude_agent_id=None, limit=16):
        params = [cutoff]
        start_exclusion = ""
        finish_exclusion = ""
        if exclude_agent_id:
            start_exclusion = "AND s.agent_id<>? "
            finish_exclusion = "AND s.agent_id<>? "
            params.append(exclude_agent_id)
        params.append(cutoff)
        if exclude_agent_id:
            params.append(exclude_agent_id)
        params.append(limit)
        query = (
            "SELECT agent_id,event,event_at,intent FROM ("
            "SELECT s.agent_id,'started' AS event,s.registered_at AS event_at,"
            "COALESCE((SELECT e.intent FROM agent_task_events e "
            "WHERE e.agent_id=s.agent_id ORDER BY e.id ASC LIMIT 1),s.intent) AS intent "
            "FROM agent_sessions s WHERE s.registered_at>=? "
            f"{start_exclusion}"
            "UNION ALL "
            "SELECT s.agent_id,'finished' AS event,a.timestamp AS event_at,s.intent "
            "FROM agent_activity_events a JOIN agent_sessions s ON s.agent_id=a.agent_id "
            "WHERE a.tool='agent_finish' AND a.timestamp>=? "
            f"{finish_exclusion}"
            ") ORDER BY event_at DESC LIMIT ?"
        )
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (await db.execute(query, params)).fetchall()
        return [
            {"agent_id": row[0], "event": row[1], "event_at": row[2], "intent": row[3]}
            for row in rows
        ]

    async def create_message(
        self, message_hash, sender_agent_id, target_name, text, created_at, recipient_ids
    ):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            await db.execute(
                "INSERT INTO coordination_messages("
                "message_hash,sender_agent_id,target_name,text,created_at) VALUES(?,?,?,?,?)",
                (message_hash, sender_agent_id, target_name, text, created_at),
            )
            await db.executemany(
                "INSERT INTO coordination_message_recipients("
                "message_hash,recipient_agent_id,read_at) VALUES(?,?,NULL)",
                [(message_hash, recipient_id) for recipient_id in recipient_ids],
            )
            await db.commit()

    async def pending_messages(self, agent_id):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (
                await db.execute(
                    "SELECT m.message_hash,m.sender_agent_id,m.target_name,m.text,m.created_at "
                    "FROM coordination_message_recipients r "
                    "JOIN coordination_messages m ON m.message_hash=r.message_hash "
                    "WHERE r.recipient_agent_id=? AND r.read_at IS NULL "
                    "ORDER BY m.created_at,m.rowid",
                    (agent_id,),
                )
            ).fetchall()
        return [
            {
                "message_hash": row[0],
                "sender_agent_id": row[1],
                "target_name": row[2],
                "text": row[3],
                "created_at": row[4],
            }
            for row in rows
        ]

    async def acknowledge_message(self, message_hash, agent_id, read_at):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            message = await (
                await db.execute(
                    "SELECT sender_agent_id FROM coordination_messages WHERE message_hash=?",
                    (message_hash,),
                )
            ).fetchone()
            if message is None:
                return False
            recipient = await (
                await db.execute(
                    "SELECT read_at FROM coordination_message_recipients "
                    "WHERE message_hash=? AND recipient_agent_id=?",
                    (message_hash, agent_id),
                )
            ).fetchone()
            if recipient is not None:
                await db.execute(
                    "UPDATE coordination_message_recipients "
                    "SET read_at=COALESCE(read_at,?) "
                    "WHERE message_hash=? AND recipient_agent_id=?",
                    (read_at, message_hash, agent_id),
                )
                await db.commit()
                return True
            return message[0] == agent_id

    async def message_readers(self, message_hash):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (
                await db.execute(
                    "SELECT recipient_agent_id FROM coordination_message_recipients "
                    "WHERE message_hash=? AND read_at IS NOT NULL ORDER BY read_at,recipient_agent_id",
                    (message_hash,),
                )
            ).fetchall()
        return [row[0] for row in rows]

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
            "details": json.loads(row[7]),
            "current_step": int(row[8]),
        }
