import { expect, test } from 'vitest'
import type { ConsoleSnapshotReadModel } from '../api/models'
import { applyPersistentMutationResult, applyRealtimeFrame, boundedBackoffDelay, createRealtimeState, decodeRealtimeFrame, replaceSnapshot, socketOpened } from './state'

const snap = (seq: number): ConsoleSnapshotReadModel => ({
  highWaterSeq: seq, replayFromSeq: seq, duplicateEventsPossible: true,
  instance: { application:'terminal-mcp', version:'0.10.1', publicBaseUrl:'https://terminal.example', healthy:true, health:{ok:true},resources:{status:'unavailable',cpu:{status:'unavailable'},memory:{status:'unavailable'},filesystem:{status:'unavailable'},uptime:{status:'unavailable'}} },
  agents:[], tasks:[], contexts:[], communications:[],
})
const evt = (seq: number) => ({ type:'event' as const, event:{ seq, eventType:'task.updated', entityType:'task', entityId:'x', payload:{}, createdAt:'2026-09-28T09:00:00Z' } })

test('cursor reducer dedupes ordered replay and flags gaps', () => {
  let state = socketOpened(replaceSnapshot(createRealtimeState(), snap(10), true))
  state = applyRealtimeFrame(state, evt(11))
  expect(state).toMatchObject({status:'live', cursor:11, freshness:'catching_up', catchingUpScopes:['task'], staleReason:undefined})
  expect(applyRealtimeFrame(state, evt(11)).cursor).toBe(11)
  expect(applyRealtimeFrame(state, evt(13))).toMatchObject({status:'stale', freshness:'stale', cursor:11, staleReason:'cursor_gap'})
  expect(replaceSnapshot(state, snap(11), false)).toMatchObject({status:'live', freshness:'fresh', catchingUpScopes:[]})
})


test('successful persistent mutations overlay authority state immediately without dropping live connectivity', () => {
  const base = snap(10)
  base.persistent = {
    enabled: true, available: true, serverNow: '2026-10-01T12:00:00Z',
    policy: {
      durationSeconds: 1380, warningAfterSeconds: 1200, alertAfterSeconds: 1320,
      rearmAfterSeconds: 180, manualRearm: true, admissionMode: 'bearer',
      legacyAdmissionEnabled: false, policyControlSupported: true,
    },
    slots: [{
      logicalAgentId: 'la-1', displayName: 'Oscar', state: 'suspended', authorityNodeId: 'secondary',
      authorityEpoch: 2, slotRevision: 7, selector: 'ABCD', selectorGeneration: 1, authGeneration: 1,
      createdAt: '2026-10-01T11:00:00Z', updatedAt: '2026-10-01T11:30:00Z', serverNow: '2026-10-01T12:00:00Z',
      claims: [], audit: [], attachments: [],
    }],
  }
  let state = socketOpened(replaceSnapshot(createRealtimeState(), base, true))
  state = applyPersistentMutationResult(state, {
    ok: true,
    payload: {
      ok: true,
      slot: {
        logical_agent_id: 'la-1', display_name: 'Oscar', state: 'armed', authority_node_id: 'secondary',
        authority_epoch: 2, slot_revision: 8, selector_generation: 1, auth_generation: 1,
        created_at: '2026-10-01T11:00:00Z', updated_at: '2026-10-01T12:00:01Z',
      },
      server_now: '2026-10-01T12:00:01Z',
    },
  })
  expect(state).toMatchObject({ status: 'live', freshness: 'catching_up', catchingUpScopes: ['logical_agent'] })
  expect(state.snapshot?.persistent?.slots[0]).toMatchObject({ state: 'armed', slotRevision: 8 })

  state = applyPersistentMutationResult(state, {
    ok: true,
    payload: { ok: true, policy: { rearm_after_seconds: 240, legacy_admission_enabled: true } },
  })
  expect(state.snapshot?.persistent?.policy).toMatchObject({ rearmAfterSeconds: 240, legacyAdmissionEnabled: true })
  expect(state.catchingUpScopes).toEqual(['logical_agent', 'persistent_policy'])

  state = applyPersistentMutationResult(state, {
    ok: true,
    payload: {
      ok: true,
      slot: {
        logical_agent_id: 'la-1', display_name: 'Oscar', state: 'deleted', authority_node_id: 'secondary',
        authority_epoch: 2, slot_revision: 9, selector_generation: 1, auth_generation: 1,
        created_at: '2026-10-01T11:00:00Z', updated_at: '2026-10-01T12:00:02Z',
      },
      server_now: '2026-10-01T12:00:02Z',
    },
  })
  expect(state.snapshot?.persistent?.slots).toEqual([])
  expect(state.status).toBe('live')
})

test('wire decoder and bounded backoff follow frozen M0 protocol', () => {
  expect(decodeRealtimeFrame({type:'heartbeat', cursor:7, high_water_seq:9})).toEqual({type:'heartbeat', cursor:7, highWaterSeq:9})
  expect(decodeRealtimeFrame({type:'resync_required', reason:'journal_gap', cursor:7, oldest_seq:9, high_water_seq:12})).toMatchObject({type:'resync_required', reason:'journal_gap', highWaterSeq:12})
  expect(() => decodeRealtimeFrame({type:'event',event:{seq:'bad'}})).toThrow(/Invalid realtime frame/)
  expect([0,1,2,9].map((n)=>boundedBackoffDelay(n))).toEqual([250,500,1000,8000])
})
