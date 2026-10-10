import json,sqlite3,datetime,pathlib
utc=datetime.timezone.utc
def stamp(v): return datetime.datetime.fromtimestamp(v,utc).isoformat(timespec='milliseconds')
start=datetime.datetime(2026,10,10,20,54,tzinfo=utc).timestamp(); end=datetime.datetime(2026,10,10,21,5,tzinfo=utc).timestamp()
groups={}
def group(kind,value):
 if value is None:return None
 g=groups.setdefault(kind,{})
 key=json.dumps(value,sort_keys=True)
 if key not in g:g[key]=f'{kind}{len(g)+1}'
 return g[key]
rows=[]
for line in pathlib.Path('/var/log/caddy/terminal-mcp-access.log').open():
 try:r=json.loads(line)
 except (ValueError,TypeError):continue
 t=r.get('ts',0);d=r.get('duration',0)
 if not(start<=t<=end):continue
 q=r.get('request',{});h={k.lower():v for k,v in q.get('headers',{}).items()}
 if '/mcp' not in q.get('uri',''):continue
 rows.append(dict(log_ts=stamp(t),start=stamp(t-d),duration_s=d,method=q.get('method'),path=q.get('uri','').split('?')[0],status=r.get('status'),read_bytes=r.get('bytes_read'),size=r.get('size'),session_group=group('S',h.get('x-openai-session')),conversation_group=group('C',h.get('x-openai-conversation-id')),trace_group=group('T',h.get('traceparent')),rpc_headers_present=sorted(k for k in h if 'request' in k or 'trace' in k or 'openai' in k)))
print('HTTP_SAFE',json.dumps(rows,sort_keys=True))
p='/var/lib/terminal-mcp/terminal-mcp.sqlite3'; c=sqlite3.connect('file:'+p+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
cmds=[]
for r0 in c.execute("select hash,cmd,status,exit_code,error,queue_id,queue_sequence,enqueued_at,claimed_at,started_at,finished_at from commands where hash in ('70d86f06','cac560e1')"):
 r=dict(r0);r['command_equality_group']=group('Q',r.pop('cmd'));cmds.append(r)
print('COMMANDS_SAFE',json.dumps(cmds,sort_keys=True))
for db in pathlib.Path('/var/lib/terminal-mcp').glob('*.sqlite3'):
 if 'auth' in db.name or 'projection' in db.name:continue
 try:
  conn=sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True)
  for (name,) in conn.execute('select name from sqlite_master where type="table"'):
   if 'task' in name or 'receipt' in name:
    cols=[r[1] for r in conn.execute('pragma table_info("'+name+'")')]
    print('SCHEMA',db.name,name,json.dumps(cols))
 except sqlite3.Error as e:print('DB_ERROR',db.name,type(e).__name__)
