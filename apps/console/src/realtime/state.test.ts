import { expect,test } from 'vitest'
import type { ConsoleSnapshotReadModel } from '../api/models'
import { applyPersistentMutationResult,applyRealtimeFrame,createRealtimeState,replaceSnapshot,socketOpened } from './state'

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

})
