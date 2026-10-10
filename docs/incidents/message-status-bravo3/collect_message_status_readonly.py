"""Read-only snapshot of historical FirstByte message delivery observations."""

import json
import sqlite3

c = sqlite3.connect("file:/var/lib/terminal-mcp/terminal-mcp.sqlite3?mode=ro", uri=True)
c.row_factory = sqlite3.Row
for table in ("coordination_messages", "coordination_message_recipients"):
    print("SCHEMA", table, [row[1] for row in c.execute(f"PRAGMA table_info({table})")])

message_query = (
    "select message_hash,sender_agent_id,target_name,created_at,delivery_mode "
    "from coordination_messages where text like '%MSG-FB-A-0fb92d-01%' "
    "or text like '%MSG-FB-B-2f29-01%'"
)
recipient_query = (
    "select message_hash,recipient_agent_id,first_seen_at,last_seen_at,"
    "seen_count,read_at,replied_at,reply_message_hash "
    "from coordination_message_recipients where message_hash=?"
)
join_query = (
    "select r.first_seen_at,r.read_at,r.replied_at from coordination_messages m "
    "left join coordination_message_recipients r on r.message_hash=m.message_hash "
    "and r.recipient_agent_id=? where m.message_hash=?"
)
for original in c.execute(message_query):
    message = dict(original)
    recipients = [dict(row) for row in c.execute(recipient_query, (message["message_hash"],))]
    print("MESSAGE", json.dumps(message, sort_keys=True))
    print("RECIPIENT_FACTS", json.dumps(recipients, sort_keys=True))
    # Reproduce the current read-only sender JOIN without constructing runtime stores.
    sender = c.execute(
        join_query,
        (message["sender_agent_id"], message["message_hash"]),
    ).fetchone()
    print("CURRENT_SENDER_JOIN", json.dumps(dict(sender), sort_keys=True))
