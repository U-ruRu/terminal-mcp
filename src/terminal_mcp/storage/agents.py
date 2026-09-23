# ruff: noqa: E501
from __future__ import annotations

import json
import secrets

import aiosqlite

_SESSION_COLUMNS = (
    "agent_id,registered_at,last_activity_at,task_summary,intent,work_scope,state,"
    "details,current_step,ended_at,end_reason,preferred_queue_id"
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
                "details,current_step,ended_at,end_reason,preferred_queue_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
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
                "work_scope=?,details=?,current_step=? WHERE agent_id=? AND state='active'",
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

    async def end(self, agent_id, state, reason, now):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            cur = await db.execute(
                "UPDATE agent_sessions SET state=?,ended_at=COALESCE(ended_at,?),end_reason=COALESCE(end_reason,?) "
                "WHERE agent_id=? AND state='active'",
                (state, now, reason, agent_id),
            )
            await db.commit()
            return cur.rowcount == 1

    async def expire(self, agent_id, reason="idle_timeout", now=None):
        from terminal_mcp.core.orchestration import utc_text

        return await self.end(agent_id, "forced", reason, now or utc_text())

    async def finish(self, agent_id, now=None):
        from terminal_mcp.core.orchestration import utc_text

        return await self.end(agent_id, "finished", "explicit", now or utc_text())

    async def set_preferred_queue(self, agent_id, queue_id):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            cur = await db.execute(
                "UPDATE agent_sessions SET preferred_queue_id=? WHERE agent_id=? AND state='active'",
                (queue_id, agent_id),
            )
            await db.commit()
            return cur.rowcount == 1

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

    async def activity_count(self, agent_id):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            row = await (
                await db.execute(
                    "SELECT COUNT(*) FROM agent_activity_events WHERE agent_id=?", (agent_id,)
                )
            ).fetchone()
        return int(row[0])

    async def latest_activity(self, agent_id):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            row = await (
                await db.execute(
                    "SELECT timestamp,tool,command_hash FROM agent_activity_events WHERE agent_id=? "
                    "ORDER BY id DESC LIMIT 1",
                    (agent_id,),
                )
            ).fetchone()
        return {"timestamp": row[0], "tool": row[1], "command_hash": row[2]} if row else None

    async def latest_task_at(self, agent_id):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            row = await (
                await db.execute(
                    "SELECT timestamp FROM agent_task_events WHERE agent_id=? ORDER BY id DESC LIMIT 1",
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

    async def recent_sessions(self, cutoff, limit=100):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (
                await db.execute(
                    f"SELECT {_SESSION_COLUMNS} FROM agent_sessions WHERE last_activity_at>=? "
                    "ORDER BY last_activity_at DESC LIMIT ?",
                    (cutoff, limit),
                )
            ).fetchall()
        return [self._session(row) for row in rows]

    async def history_sessions(self, cutoff, limit=100):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (
                await db.execute(
                    f"SELECT {_SESSION_COLUMNS} FROM agent_sessions "
                    "WHERE registered_at>=? OR last_activity_at>=? OR ended_at>=? "
                    "ORDER BY COALESCE(ended_at,last_activity_at) DESC LIMIT ?",
                    (cutoff, cutoff, cutoff, limit),
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
            "SELECT s.agent_id,s.intent,s.registered_at,s.last_activity_at,a.command_hash,"
            "c.started_at,c.finished_at,c.status,c.queue_id "
            "FROM agent_sessions s "
            "LEFT JOIN command_agent_attribution a ON a.rowid=("
            "SELECT a2.rowid FROM command_agent_attribution a2 "
            "WHERE a2.agent_id=s.agent_id ORDER BY a2.created_at DESC,a2.rowid DESC LIMIT 1) "
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
                "last_activity_at": row[3],
                "last_command_hash": row[4],
                "last_command_started_at": row[5],
                "last_command_finished_at": row[6],
                "last_command_status": row[7],
                "last_command_queue_id": row[8],
            }
            for row in rows
        ]

    async def recent_lifecycle(self, cutoff, exclude_agent_id=None, limit=16):
        params = [cutoff]
        start_exclusion = ""
        end_exclusion = ""
        if exclude_agent_id:
            start_exclusion = "AND s.agent_id<>? "
            end_exclusion = "AND s.agent_id<>? "
            params.append(exclude_agent_id)
        params.append(cutoff)
        if exclude_agent_id:
            params.append(exclude_agent_id)
        params.append(limit)
        query = (
            "SELECT agent_id,event,event_at,intent FROM ("
            "SELECT s.agent_id,'started' AS event,s.registered_at AS event_at,"
            "COALESCE((SELECT e.intent FROM agent_task_events e WHERE e.agent_id=s.agent_id ORDER BY e.id ASC LIMIT 1),s.intent) AS intent "
            "FROM agent_sessions s WHERE s.registered_at>=? "
            f"{start_exclusion}"
            "UNION ALL "
            "SELECT s.agent_id,s.state AS event,s.ended_at AS event_at,s.intent "
            "FROM agent_sessions s WHERE s.ended_at>=? "
            f"{end_exclusion}"
            ") ORDER BY event_at DESC LIMIT ?"
        )
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (await db.execute(query, params)).fetchall()
        return [
            {"agent_id": row[0], "event": row[1], "event_at": row[2], "intent": row[3]}
            for row in rows
        ]

    async def create_message(
        self,
        message_hash,
        sender_agent_id,
        target_name,
        text,
        created_at,
        recipient_ids,
        require_reply=False,
        alert=False,
    ):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            await db.execute(
                "INSERT INTO coordination_messages("
                "message_hash,sender_agent_id,target_name,text,created_at,require_reply,alert) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    message_hash,
                    sender_agent_id,
                    target_name,
                    text,
                    created_at,
                    int(bool(require_reply or alert)),
                    int(bool(alert)),
                ),
            )
            await db.executemany(
                "INSERT INTO coordination_message_recipients("
                "message_hash,recipient_agent_id,delivered_at,first_seen_at,last_seen_at,seen_count,"
                "read_at,replied_at,reply_message_hash) VALUES(?,?,?,NULL,NULL,0,NULL,NULL,NULL)",
                [(message_hash, recipient_id, created_at) for recipient_id in recipient_ids],
            )
            await db.commit()

    async def message_record(self, message_hash):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            row = await (
                await db.execute(
                    "SELECT message_hash,sender_agent_id,target_name,text,created_at,require_reply,alert "
                    "FROM coordination_messages WHERE message_hash=?",
                    (message_hash,),
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "message_hash": row[0],
            "sender_agent_id": row[1],
            "target_name": row[2],
            "text": row[3],
            "created_at": row[4],
            "require_reply": bool(row[5]),
            "alert": bool(row[6]),
        }

    async def recipient_record(self, message_hash, agent_id):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            row = await (
                await db.execute(
                    "SELECT delivered_at,first_seen_at,last_seen_at,seen_count,read_at,replied_at,reply_message_hash "
                    "FROM coordination_message_recipients WHERE message_hash=? AND recipient_agent_id=?",
                    (message_hash, agent_id),
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "delivered_at": row[0],
            "first_seen_at": row[1],
            "last_seen_at": row[2],
            "seen_count": int(row[3] or 0),
            "read_at": row[4],
            "replied_at": row[5],
            "reply_message_hash": row[6],
        }

    async def message_obligations(self, agent_id):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (
                await db.execute(
                    "SELECT m.message_hash,m.sender_agent_id,m.target_name,m.text,m.created_at,"
                    "m.require_reply,m.alert,r.delivered_at,r.first_seen_at,r.last_seen_at,r.seen_count,"
                    "r.read_at,r.replied_at,r.reply_message_hash "
                    "FROM coordination_message_recipients r "
                    "JOIN coordination_messages m ON m.message_hash=r.message_hash "
                    "WHERE r.recipient_agent_id=? AND (r.read_at IS NULL OR "
                    "((m.require_reply=1 OR m.alert=1) AND r.replied_at IS NULL)) "
                    "ORDER BY m.alert DESC,m.created_at,m.rowid",
                    (agent_id,),
                )
            ).fetchall()
        keys = (
            "message_hash",
            "sender_agent_id",
            "target_name",
            "text",
            "created_at",
            "require_reply",
            "alert",
            "delivered_at",
            "first_seen_at",
            "last_seen_at",
            "seen_count",
            "read_at",
            "replied_at",
            "reply_message_hash",
        )
        result = []
        for row in rows:
            item = dict(zip(keys, row, strict=True))
            item["require_reply"] = bool(item["require_reply"])
            item["alert"] = bool(item["alert"])
            item["seen_count"] = int(item["seen_count"] or 0)
            result.append(item)
        return result

    async def message_journal(self, agent_id, cutoff=None, limit=100):
        condition = "r.recipient_agent_id=?"
        params = [agent_id]
        if cutoff:
            condition += " AND m.created_at>=?"
            params.append(cutoff)
        params.append(limit)
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (
                await db.execute(
                    "SELECT m.message_hash,m.sender_agent_id,m.target_name,m.text,m.created_at,"
                    "m.require_reply,m.alert,r.delivered_at,r.first_seen_at,r.last_seen_at,r.seen_count,"
                    "r.read_at,r.replied_at,r.reply_message_hash "
                    "FROM coordination_message_recipients r "
                    "JOIN coordination_messages m ON m.message_hash=r.message_hash "
                    f"WHERE {condition} ORDER BY m.created_at DESC,m.rowid DESC LIMIT ?",
                    params,
                )
            ).fetchall()
        keys = (
            "message_hash",
            "sender_agent_id",
            "target_name",
            "text",
            "created_at",
            "require_reply",
            "alert",
            "delivered_at",
            "first_seen_at",
            "last_seen_at",
            "seen_count",
            "read_at",
            "replied_at",
            "reply_message_hash",
        )
        result = []
        for row in rows:
            item = dict(zip(keys, row, strict=True))
            item["require_reply"] = bool(item["require_reply"])
            item["alert"] = bool(item["alert"])
            item["seen_count"] = int(item["seen_count"] or 0)
            result.append(item)
        return result

    async def ensure_system_alert(
        self,
        *,
        sender_agent_id,
        recipient_agent_id,
        target_name,
        text,
        now,
        repeat_cutoff,
    ):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            await db.execute("BEGIN IMMEDIATE")
            latest = await (
                await db.execute(
                    "SELECT m.message_hash,m.created_at,r.replied_at FROM coordination_message_recipients r "
                    "JOIN coordination_messages m ON m.message_hash=r.message_hash "
                    "WHERE r.recipient_agent_id=? AND m.sender_agent_id=? AND m.alert=1 "
                    "ORDER BY m.created_at DESC,m.rowid DESC LIMIT 1",
                    (recipient_agent_id, sender_agent_id),
                )
            ).fetchone()
            if latest is not None:
                message_hash, created_at, replied_at = latest
                if replied_at is None or created_at > repeat_cutoff:
                    await db.commit()
                    return {"message_hash": message_hash, "created": False}

            for _ in range(32):
                message_hash = secrets.token_hex(4)
                exists = await (
                    await db.execute(
                        "SELECT 1 FROM coordination_messages WHERE message_hash=?", (message_hash,)
                    )
                ).fetchone()
                if exists is not None:
                    continue
                await db.execute(
                    "INSERT INTO coordination_messages("
                    "message_hash,sender_agent_id,target_name,text,created_at,require_reply,alert) "
                    "VALUES(?,?,?,?,?,1,1)",
                    (message_hash, sender_agent_id, target_name, text, now),
                )
                await db.execute(
                    "INSERT INTO coordination_message_recipients("
                    "message_hash,recipient_agent_id,delivered_at,first_seen_at,last_seen_at,seen_count,"
                    "read_at,replied_at,reply_message_hash) VALUES(?,?,?,NULL,NULL,0,NULL,NULL,NULL)",
                    (message_hash, recipient_agent_id, now),
                )
                await db.commit()
                return {"message_hash": message_hash, "created": True}
            await db.rollback()
            raise RuntimeError("unable to allocate unique system alert hash")

    async def mark_messages_seen(self, agent_id, message_hashes, seen_at):
        hashes = list(dict.fromkeys(message_hashes))
        if not hashes:
            return
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            for message_hash in hashes:
                await db.execute(
                    "UPDATE coordination_message_recipients SET "
                    "first_seen_at=COALESCE(first_seen_at,?),last_seen_at=?,seen_count=seen_count+1 "
                    "WHERE message_hash=? AND recipient_agent_id=?",
                    (seen_at, seen_at, message_hash, agent_id),
                )
            await db.commit()

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
                    "UPDATE coordination_message_recipients SET read_at=COALESCE(read_at,?) "
                    "WHERE message_hash=? AND recipient_agent_id=?",
                    (read_at, message_hash, agent_id),
                )
                await db.commit()
                return True
            return message[0] == agent_id

    async def mark_replied(self, message_hash, agent_id, reply_hash, replied_at):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            cur = await db.execute(
                "UPDATE coordination_message_recipients SET read_at=COALESCE(read_at,?),"
                "replied_at=COALESCE(replied_at,?),reply_message_hash=COALESCE(reply_message_hash,?) "
                "WHERE message_hash=? AND recipient_agent_id=?",
                (replied_at, replied_at, reply_hash, message_hash, agent_id),
            )
            await db.commit()
            return cur.rowcount == 1

    async def message_receipts(self, message_hash):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (
                await db.execute(
                    "SELECT recipient_agent_id,first_seen_at,read_at,replied_at "
                    "FROM coordination_message_recipients WHERE message_hash=? "
                    "ORDER BY delivered_at,recipient_agent_id",
                    (message_hash,),
                )
            ).fetchall()
        return [
            {
                "agent_id": row[0],
                "seen": row[1] is not None,
                "read": row[2] is not None,
                "replied": row[3] is not None,
            }
            for row in rows
        ]

    async def pending_messages(self, agent_id):
        return await self.message_obligations(agent_id)

    async def message_readers(self, message_hash):
        receipts = await self.message_receipts(message_hash)
        return [item["agent_id"] for item in receipts if item["read"]]

    async def recent_commands(self, agent_id, limit=3):
        rows = await self.command_journal(agent_id, None, limit)
        return rows

    async def command_journal(self, agent_id, cutoff=None, limit=100):
        condition = "a.agent_id=?"
        params = [agent_id]
        if cutoff:
            condition += " AND a.created_at>=?"
            params.append(cutoff)
        params.append(limit)
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (
                await db.execute(
                    "SELECT a.created_at,a.command_hash,a.command_preview,c.status,a.command_type,"
                    "c.queue_id,c.queue_sequence,c.started_at,c.finished_at "
                    "FROM command_agent_attribution a JOIN commands c ON c.hash=a.command_hash "
                    f"WHERE {condition} ORDER BY a.created_at DESC,a.rowid DESC LIMIT ?",
                    params,
                )
            ).fetchall()
        return [
            {
                "created_at": r[0],
                "command_hash": r[1],
                "preview": r[2],
                "status": r[3],
                "command_type": r[4],
                "queue_id": r[5],
                "queue_sequence": r[6],
                "started_at": r[7],
                "finished_at": r[8],
            }
            for r in rows
        ]

    async def command_detail(self, command_hash):
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            row = await (
                await db.execute(
                    "SELECT c.hash,c.cmd,c.status,c.queue_id,c.queue_sequence,c.enqueued_at,c.claimed_at,"
                    "c.started_at,c.finished_at,c.exit_code,c.error,a.agent_id,a.command_type,a.created_at "
                    "FROM commands c LEFT JOIN command_agent_attribution a ON a.command_hash=c.hash "
                    "WHERE c.hash=?",
                    (command_hash,),
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "command_hash": row[0],
            "cmd": row[1],
            "status": row[2],
            "queue_id": row[3],
            "queue_sequence": row[4],
            "enqueued_at": row[5],
            "claimed_at": row[6],
            "started_at": row[7],
            "finished_at": row[8],
            "exit_code": row[9],
            "error": row[10],
            "agent_id": row[11],
            "command_type": row[12],
            "created_at": row[13],
        }

    async def intent_journal(self, agent_id, cutoff=None, limit=100):
        condition = "agent_id=?"
        params = [agent_id]
        if cutoff:
            condition += " AND timestamp>=?"
            params.append(cutoff)
        params.append(limit)
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            rows = await (
                await db.execute(
                    "SELECT timestamp,intent,step,work_scope FROM agent_task_events "
                    f"WHERE {condition} ORDER BY id DESC LIMIT ?",
                    params,
                )
            ).fetchall()
        return [
            {
                "timestamp": row[0],
                "intent": row[1],
                "step": int(row[2]),
                "work_scope": json.loads(row[3]),
            }
            for row in rows
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
            "ended_at": row[9],
            "end_reason": row[10],
            "preferred_queue_id": row[11],
        }
