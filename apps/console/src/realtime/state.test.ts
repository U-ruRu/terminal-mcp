import { expect,test } from 'vitest'
import type { ConsoleSnapshotReadModel } from '../api/models'
import { applyRealtimeFrame,createRealtimeState,replaceSnapshot,socketOpened } from './state'

const snap = (seq: number): ConsoleSnapshotReadModel => ({
  highWaterSeq: seq, replayFromSeq: seq, duplicateEventsPossible: true,
  instance: { application:'terminal-mcp', version:'0.14.4', publicBaseUrl:'https://terminal.example', healthy:true, health:{ok:true},resources:{status:'unavailable',cpu:{status:'unavailable'},memory:{status:'unavailable'},filesystem:{status:'unavailable'},uptime:{status:'unavailable'}} },
  agents:[], tasks:[], contexts:[], communications:[],
})
const evt = (seq: number) => ({ type:'event' as const, event:{ seq, eventType:'task.updated', entityType:'task', entityId:'x', payload:{}, createdAt:'2026-09-28T09:00:00Z' } })

test('the next durable event advances the cursor and marks its scope as catching up', () => {
  const prior = socketOpened(replaceSnapshot(createRealtimeState(), snap(10), true))

  const state = applyRealtimeFrame(prior, evt(11))

  expect(state).toMatchObject({status:'live', cursor:11, freshness:'catching_up', catchingUpScopes:['task']})
})
