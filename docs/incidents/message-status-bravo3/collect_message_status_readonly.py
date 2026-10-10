import sqlite3,json
c=sqlite3.connect('file:/var/lib/terminal-mcp/terminal-mcp.sqlite3?mode=ro',uri=True);c.row_factory=sqlite3.Row
for t in ('coordination_messages','coordination_message_recipients'):
 print('SCHEMA',t,[r[1] for r in c.execute('pragma table_info('+t+')')])
for m0 in c.execute("select message_hash,sender_agent_id,target_name,created_at,delivery_mode from coordination_messages where text like '%MSG-FB-A-0fb92d-01%' or text like '%MSG-FB-B-2f29-01%'"):
 m=dict(m0)
 rec=[dict(r) for r in c.execute('select message_hash,recipient_agent_id,first_seen_at,last_seen_at,seen_count,read_at,replied_at,reply_message_hash from coordination_message_recipients where message_hash=?',(m['message_hash'],))]
 print('MESSAGE',json.dumps(m,sort_keys=True));print('RECIPIENT_FACTS',json.dumps(rec,sort_keys=True))
 # Reproduce the current read-only sender JOIN without constructing runtime stores.
 r=c.execute('select r.first_seen_at,r.read_at,r.replied_at from coordination_messages m left join coordination_message_recipients r on r.message_hash=m.message_hash and r.recipient_agent_id=? where m.message_hash=?',(m['sender_agent_id'],m['message_hash'])).fetchone()
 print('CURRENT_SENDER_JOIN',json.dumps(dict(r),sort_keys=True))
