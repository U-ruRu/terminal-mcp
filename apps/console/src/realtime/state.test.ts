import { expect, test } from 'vitest'
import type { ConsoleSnapshotReadModel } from '../api/models'
import { applyRealtimeFrame, boundedBackoffDelay, createRealtimeState, decodeRealtimeFrame, replaceSnapshot, socketOpened } from './state'

const snap = (seq: number): ConsoleSnapshotReadModel => ({
  highWaterSeq: seq, replayFromSeq: seq, duplicateEventsPossible: true,
  instance: { application:'terminal-mcp', version:'0.10.1', publicBaseUrl:'https://terminal.example', healthy:true, health:{ok:true},resources:{status:'unavailable',cpu:{status:'unavailable'},memory:{status:'unavailable'},filesystem:{status:'unavailable'},uptime:{status:'unavailable'}} },
  agents:[], tasks:[], contexts:[], communications:[],
})
const evt = (seq: number) => ({ type:'event' as const, event:{ seq, eventType:'task.updated', entityType:'task', entityId:'x', payload:{}, createdAt:'2026-09-28T09:00:00Z' } })

test('cursor reducer dedupes ordered replay and flags gaps', () => {
  let state = socketOpened(replaceSnapshot(createRealtimeState(), snap(10), true))
  state = applyRealtimeFrame(state, evt(11))
  expect(state).toMatchObject({status:'stale', cursor:11, staleReason:'event_pending_refresh'})
  expect(applyRealtimeFrame(state, evt(11)).cursor).toBe(11)
  expect(applyRealtimeFrame(state, evt(13))).toMatchObject({cursor:11, staleReason:'cursor_gap'})
  expect(replaceSnapshot(state, snap(11), false).status).toBe('live')
})

test('wire decoder and bounded backoff follow frozen M0 protocol', () => {
  expect(decodeRealtimeFrame({type:'heartbeat', cursor:7, high_water_seq:9})).toEqual({type:'heartbeat', cursor:7, highWaterSeq:9})
  expect(decodeRealtimeFrame({type:'resync_required', reason:'journal_gap', cursor:7, oldest_seq:9, high_water_seq:12})).toMatchObject({type:'resync_required', reason:'journal_gap', highWaterSeq:12})
  expect(() => decodeRealtimeFrame({type:'event',event:{seq:'bad'}})).toThrow(/Invalid realtime frame/)
  expect([0,1,2,9].map((n)=>boundedBackoffDelay(n))).toEqual([250,500,1000,8000])
})
