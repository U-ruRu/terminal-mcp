import { expect, test } from 'vitest'
import type { ConnectionProfile } from '../connections/types'
import { buildProjectedFleetInstances } from './projectionAdapter'
import type { FleetCacheView, FleetProjectionEntity } from './v1Types'

const profile: ConnectionProfile = {
  instanceId: 'profile-a',
  origin: 'https://node-a.example',
  displayName: 'Node A',
  credentialRef: 'cred-a',
  metadata: { deviceId: 'd', clientId: 'c', deviceLabel: 'Console', scope: 'terminal:read', pairedAt: 1 },
  createdAt: 1,
  updatedAt: 1,
}
function entity(type: string, id: string, payload: Record<string, unknown>): FleetProjectionEntity {
  return {
    sourceNodeId: 'node-a', entityType: type, entityId: id, entityRevision: 10,
    payloadVersion: 2, payload, sourceStreamGeneration: 'g1', sourceSeq: 10,
    projectionSeq: 10, updatedAt: '2026-09-30T19:00:00Z',
  }
}
function cache(
  values: FleetProjectionEntity[],
  runtimeOverlays: FleetCacheView['runtimeOverlays'] = [],
): FleetCacheView {
  return {
    version: 1, fleetId: 'fleet-a', projectionEpoch: 2, appliedProjectionSeq: 10,
    entities: values,
    sources: [{
      sourceNodeId: 'node-a', sourceStreamGeneration: 'g1', sourceSeq: 10,
      freshness: 'fresh', updatedAt: '2026-09-30T19:00:00Z',
    }],
    activity: [], gatewayQuality: [], updatedAt: 1, runtimeOverlays,
  }
}

test('projected claim stays visible with no active work session', () => {
  const instances = buildProjectedFleetInstances(cache([
    entity('logical_agent', 'logical-1', {
      logical_agent_id: 'logical-1', display_name: 'Oscar', state: 'suspended',
      authority_node_id: 'node-a', authority_epoch: 2, slot_revision: 7,
      selector_generation: 3, auth_generation: 4,
      created_at: '2026-09-30T18:00:00Z', updated_at: '2026-09-30T19:00:00Z',
    }),
    entity('task', 'terminal/T-1', {
      namespace: 'terminal', task_id: 'T-1', title: 'Durable',
      lane: 'implementation', priority: 3, state: 'ready', revision: 2, tags: [],
    }),
    entity('work_claim', '11', {
      namespace: 'terminal', task_id: 'T-1', owner_kind: 'logical_agent',
      owner_id: 'logical-1', claimed_at: '2026-09-30T18:30:00Z',
      released_at: null, claim_intent: 'continue',
    }),
  ]), [{ sourceNodeId: 'node-a', profile }], 'degraded')

  const slot = instances[0].runtime.realtime?.snapshot?.persistent?.slots[0]
  expect(slot?.workSession).toBeUndefined()
  expect(slot?.claims[0]).toMatchObject({ taskId: 'T-1', priority: 'P0', claimIntent: 'continue' })
  expect(instances[0].runtime.status).toBe('stale')
})

test('hard expiry and session epoch survive adaptation literally while policy/selector stay unavailable', () => {
  const hard = '2026-09-30T20:23:00Z'
  const instances = buildProjectedFleetInstances(cache([
    entity('logical_agent', 'logical-1', {
      logical_agent_id: 'logical-1', display_name: 'Oscar', state: 'active',
      authority_node_id: 'node-a', authority_epoch: 2, slot_revision: 8,
      selector_generation: 3, auth_generation: 4,
      created_at: '2026-09-30T18:00:00Z', updated_at: '2026-09-30T19:00:00Z',
    }),
    entity('work_session', 'ws-1', {
      work_session_id: 'ws-1', logical_agent_id: 'logical-1', session_epoch: 9,
      authority_node_id: 'node-a', authority_epoch: 2,
      started_at: '2026-09-30T20:00:00Z', hard_expires_at: hard,
      state: 'active', origin_instance_id: 'node-a', ended_at: null,
    }),
  ]), [{ sourceNodeId: 'node-a', profile }], 'live', Date.parse('2026-09-30T20:01:00Z'))

  const persistent = instances[0].runtime.realtime?.snapshot?.persistent
  expect(persistent?.slots[0].workSession).toMatchObject({
    workSessionId: 'ws-1', sessionEpoch: 9, hardExpiresAt: hard,
  })
  expect(persistent?.slots[0].selector).toBe('—')
  expect(persistent?.policy.projected).toBe(true)
  expect(persistent?.policy.policyControlSupported).toBe(false)
})

test('fresh unowned overlay degrades source without terminalizing durable command', () => {
  const instances = buildProjectedFleetInstances(
    cache(
      [entity('command', 'cmd-1', { command_hash: 'cmd-1', status: 'running' })],
      [{
        sourceNodeId: 'node-a',
        payload: { unowned_running_commands: ['cmd-1'] },
        observedAt: '2026-09-30T19:00:01Z',
        freshness: 'fresh',
      }],
    ),
    [{ sourceNodeId: 'node-a', profile }],
    'live',
  )
  expect(instances[0].runtime.status).toBe('stale')
  expect(instances[0].runtime.realtime?.snapshot?.instance.healthy).toBe(false)
})

test('fresh sampled resource overlay is normalized and ages out', () => {
  const now = Date.parse('2026-09-30T19:00:05Z')
  const fresh = cache([], [{
    sourceNodeId: 'node-a',
    payload: {
      resources: {
        status: 'available',
        cpu: { status: 'available', logical_cores: 8, load_1m: 1.5 },
        memory: { status: 'available', total_bytes: 100, used_bytes: 40, available_bytes: 60, used_percent: 40 },
        filesystem: { status: 'available', total_bytes: 200, used_bytes: 50, free_bytes: 150, used_percent: 25 },
        uptime: { status: 'available', seconds: 123 },
      },
    },
    observedAt: '2026-09-30T19:00:00Z',
    freshness: 'fresh',
  }])
  const live = buildProjectedFleetInstances(fresh, [{ sourceNodeId: 'node-a', profile }], 'live', now)
  expect(live[0].runtime.realtime?.snapshot?.instance.resources.cpu.logicalCores).toBe(8)
  expect(live[0].runtime.realtime?.snapshot?.instance.resources.memory.totalBytes).toBe(100)

  const stale = buildProjectedFleetInstances(fresh, [{ sourceNodeId: 'node-a', profile }], 'live', now + 11_000)
  expect(stale[0].runtime.realtime?.snapshot?.instance.resources.status).toBe('unavailable')
})
