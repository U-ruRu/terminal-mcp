import { expect,test,vi } from 'vitest';
import type { ConsoleSnapshotReadModel } from '../api/models';
import { ConsoleHttpError } from '../api/client';
import { ConsoleContractError } from '../api/contracts';
import { describeRealtimeFailure } from './failure';
import { RealtimeConsoleEngine,consoleEventsUrl,type RealtimeScheduler,type RealtimeSocket } from './engine';

const snap = (seq:number): ConsoleSnapshotReadModel => ({
  highWaterSeq:seq, replayFromSeq:seq, duplicateEventsPossible:true,
  instance:{application:'terminal-mcp',version:'0.14.4',publicBaseUrl:'https://terminal.example',healthy:true,health:{ok:true},resources:{status:'unavailable',cpu:{status:'unavailable'},memory:{status:'unavailable'},filesystem:{status:'unavailable'},uptime:{status:'unavailable'}}},
  agents:[],tasks:[],contexts:[],communications:[],
})
class Sock implements RealtimeSocket {
  onopen:(()=>void)|null=null; onmessage:((e:{data:string})=>void)|null=null
  onclose:((e:{code:number;reason:string;wasClean:boolean})=>void)|null=null; onerror:(()=>void)|null=null
  closed=false
  open(){this.onopen?.()} close(){this.closed=true}
  msg(v:object){this.onmessage?.({data:JSON.stringify(v)})}
  drop(){this.onclose?.({code:1006,reason:'lost',wasClean:false})}
}
class Scheduler implements RealtimeScheduler {
  q:Array<{cb:()=>void,ms:number,h:object}>=[]
  setTimeout(cb:()=>void,ms:number){const h={};this.q.push({cb,ms,h});return h}
  clearTimeout(h:unknown){this.q=this.q.filter(x=>x.h!==h)}
  run(){const x=this.q.shift();if(!x)throw new Error('empty');x.cb();return x.ms}
}

test('a durable event refreshes the snapshot to its new cursor', async()=>{
  const scheduler=new Scheduler()
  const snapshots=[snap(10),snap(11)]
  const client={
    snapshot:vi.fn(async()=>snapshots.shift()??snap(11)),
    webSocketTicket:vi.fn().mockResolvedValueOnce({ticket:'a',expiresIn:10}).mockResolvedValueOnce({ticket:'b',expiresIn:10}),
  }
  const sockets:Sock[]=[]; const urls:string[]=[]
  const engine=new RealtimeConsoleEngine(client,'https://terminal.example',{scheduler,socketFactory:(url)=>{urls.push(url);const s=new Sock();sockets.push(s);return s}})
  await engine.start(); sockets[0].open()
  sockets[0].msg({type:'event',event:{seq:11,event_type:'task.updated',entity_type:'task',entity_id:'x',payload:{},created_at:'2026-09-28T09:00:00Z'}})
  expect(engine.getState()).toMatchObject({status:'live', freshness:'catching_up', cursor:11, catchingUpScopes:['task']})
  expect(client.snapshot).toHaveBeenCalledTimes(1)
  scheduler.run(); await vi.waitFor(()=>expect(client.snapshot).toHaveBeenCalledTimes(2))
  expect(engine.getState()).toMatchObject({status:'live', freshness:'fresh', cursor:11})
  engine.stop()
})

test('journal gap resnapshots atomically and reconnects at new high water', async()=>{
  const client={
    snapshot:vi.fn().mockResolvedValueOnce(snap(4)).mockResolvedValueOnce(snap(20)),
    webSocketTicket:vi.fn().mockResolvedValueOnce({ticket:'a',expiresIn:10}).mockResolvedValueOnce({ticket:'b',expiresIn:10}),
  }
  const sockets:Sock[]=[]; const urls:string[]=[]
  const engine=new RealtimeConsoleEngine(client,'https://terminal.example',{socketFactory:(url)=>{urls.push(url);const s=new Sock();sockets.push(s);return s}})
  await engine.start(); sockets[0].open()
  sockets[0].msg({type:'resync_required',reason:'journal_gap',cursor:4,oldest_seq:10,high_water_seq:20})
  await vi.waitFor(()=>expect(sockets).toHaveLength(2))
  expect(sockets[0].closed).toBe(true)
  expect(engine.getState().cursor).toBe(20)
  expect(urls[1]).toBe(consoleEventsUrl('https://terminal.example','b',20))
  engine.stop()
})


test('initial and retry snapshot contract failures report the exact field and stage', async () => {
  const scheduler = new Scheduler()
  const error = new ConsoleContractError('$.tasks.tasks[0].state', 'expected one of ready, in_progress, qa, blocked, deferred, done')
  const client = { snapshot: vi.fn(async () => { throw error }), webSocketTicket: vi.fn() }
  const onFailure = vi.fn()
  const engine = new RealtimeConsoleEngine(client, 'https://terminal.example', { scheduler, onFailure })

  await engine.start()
  expect(onFailure).toHaveBeenCalledWith(expect.objectContaining({
    stage: 'initial_snapshot',
    code: 'console_contract_invalid',
    detail: expect.stringContaining('path=$.tasks.tasks[0].state'),
  }))
  expect(engine.getState()).toMatchObject({
    status: 'reconnecting', lastError: 'console_contract_invalid', snapshot: null,
  })

  scheduler.run()
  await vi.waitFor(() => expect(onFailure).toHaveBeenCalledWith(expect.objectContaining({
    stage: 'snapshot_resync', detail: expect.stringContaining('path=$.tasks.tasks[0].state'),
  })))
  engine.stop()
})

test('failure details include HTTP context and redact bearer credentials and URLs', () => {
  const http = describeRealtimeFailure(
    'initial_snapshot',
    new ConsoleHttpError(502, 'upstream_unavailable', 'failure', '/actions/console/snapshot', 82),
  )
  expect(http).toMatchObject({
    code: 'upstream_unavailable',
    detail: 'stage=initial_snapshot http=502 endpoint=/actions/console/snapshot elapsed_ms=82 code=upstream_unavailable',
  })

  const secret = describeRealtimeFailure('websocket_closed', new Error(
    'failed https://private.example/path?ticket=SECRET_VALUE Bearer VERY_PRIVATE_TOKEN token=PRIVATE access_token=OTHER_SECRET',
  ))
  expect(secret.detail).toContain('stage=websocket_closed')
  expect(secret.detail).not.toMatch(/SECRET_VALUE|VERY_PRIVATE_TOKEN|PRIVATE|OTHER_SECRET|private.example/)
})
