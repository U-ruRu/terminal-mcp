import { expect, test, vi } from 'vitest'
import type { ConsoleSnapshotReadModel } from '../api/models'
import { RealtimeConsoleEngine, consoleEventsUrl, type RealtimeScheduler, type RealtimeSocket } from './engine'

const snap = (seq:number): ConsoleSnapshotReadModel => ({
  highWaterSeq:seq, replayFromSeq:seq, duplicateEventsPossible:true,
  instance:{application:'terminal-mcp',version:'0.10.1',publicBaseUrl:'https://terminal.example',healthy:true,health:{ok:true},resources:{status:'unavailable',cpu:{status:'unavailable'},memory:{status:'unavailable'},filesystem:{status:'unavailable'},uptime:{status:'unavailable'}}},
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
const tick=async()=>{await Promise.resolve();await Promise.resolve();await Promise.resolve()}

test('ordered event refreshes snapshot; disconnect resumes from cursor with backoff', async()=>{
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
  await vi.waitFor(()=>expect(engine.getState().status).toBe('live'))
  expect(engine.getState().cursor).toBe(11)
  sockets[0].drop()
  expect(engine.getState()).toMatchObject({status:'reconnecting',reconnectAttempt:1})
  expect(scheduler.run()).toBe(250); await tick()
  expect(urls[1]).toBe(consoleEventsUrl('https://terminal.example','b',11))
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

test('initial snapshot failure retries with jitter and recovers without a page reload', async () => {
  const scheduler = new Scheduler()
  const client = {
    snapshot: vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(snap(5)),
    webSocketTicket: vi.fn().mockResolvedValue({ ticket: 'retry-ticket', expiresIn: 10 }),
  }
  const sockets: Sock[] = []
  const jitter = vi.fn((delayMs: number, attempt: number) => delayMs + attempt * 17)
  const engine = new RealtimeConsoleEngine(client, 'https://terminal.example', {
    scheduler,
    reconnectJitter: jitter,
    socketFactory: () => {
      const socket = new Sock()
      sockets.push(socket)
      return socket
    },
  })

  await engine.start()
  expect(engine.getState()).toMatchObject({ status: 'reconnecting', reconnectAttempt: 1 })
  expect(jitter).toHaveBeenCalledWith(250, 1)
  expect(scheduler.run()).toBe(267)

  await vi.waitFor(() => expect(sockets).toHaveLength(1))
  sockets[0].open()
  expect(engine.getState()).toMatchObject({ status: 'live', cursor: 5 })
  engine.stop()
})

test('event bursts coalesce snapshot refreshes instead of creating an unbounded request queue', async () => {
  let resolveRefresh: ((value: ConsoleSnapshotReadModel) => void) | undefined
  const pendingRefresh = new Promise<ConsoleSnapshotReadModel>((resolve) => {
    resolveRefresh = resolve
  })
  const client = {
    snapshot: vi
      .fn()
      .mockResolvedValueOnce(snap(0))
      .mockImplementationOnce(() => pendingRefresh)
      .mockResolvedValue(snap(20)),
    webSocketTicket: vi.fn().mockResolvedValue({ ticket: 'burst', expiresIn: 10 }),
  }
  const sockets: Sock[] = []
  const engine = new RealtimeConsoleEngine(client, 'https://terminal.example', {
    socketFactory: () => {
      const socket = new Sock()
      sockets.push(socket)
      return socket
    },
  })

  await engine.start()
  sockets[0].open()
  for (let seq = 1; seq <= 20; seq += 1) {
    sockets[0].msg({
      type: 'event',
      event: {
        seq,
        event_type: 'task.updated',
        entity_type: 'task',
        entity_id: String(seq),
        payload: {},
        created_at: '2026-09-28T09:00:00Z',
      },
    })
  }

  await vi.waitFor(() => expect(client.snapshot).toHaveBeenCalledTimes(2))
  resolveRefresh?.(snap(20))
  await vi.waitFor(() => expect(client.snapshot).toHaveBeenCalledTimes(3))
  await tick()

  expect(engine.getState().cursor).toBe(20)
  expect(client.snapshot).toHaveBeenCalledTimes(3)
  engine.stop()
})
