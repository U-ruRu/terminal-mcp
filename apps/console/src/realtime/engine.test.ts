import { expect,test,vi } from 'vitest';
import type { ConsoleSnapshotReadModel } from '../api/models';
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
