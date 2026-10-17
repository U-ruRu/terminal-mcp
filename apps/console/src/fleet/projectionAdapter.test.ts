import { expect,test } from 'vitest'
import type { ConnectionProfile } from '../connections/types'
import { buildProjectedFleetInstances,taskFromQueryItem } from './projectionAdapter'
import type { FleetCacheView,FleetProjectionEntity } from './v1Types'

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

  const task = instances[0].runtime.realtime?.snapshot?.tasks[0]
  expect(task).toMatchObject({ taskId: 'T-1', priority: 'P0', owner: { agentId: 'logical-1', claimIntent: 'continue' } })
})

test('stale runtime overlay preserves the cached version and resource values', () => {
  const value = cache([], [{
    sourceNodeId: 'node-a',
    payload: {
      application: 'terminal-mcp',
      version: 'current-release',
      resources: {
        status: 'available',
        cpu: { status: 'available', logical_cores: 8, load_1m: 1.5 },
        memory: { status: 'available', total_bytes: 100, used_bytes: 40, available_bytes: 60, used_percent: 40 },
        filesystem: { status: 'available', total_bytes: 200, used_bytes: 50, free_bytes: 150, used_percent: 25 },
        uptime: { status: 'available', seconds: 123 },
      },
    },
    observedAt: '2026-09-30T19:00:00Z',
    freshness: 'stale',
  }])
  value.sources[0].freshness = 'stale'

  const [instance] = buildProjectedFleetInstances(
    value, [{ sourceNodeId: 'node-a', profile }], 'live', Date.parse('2026-09-30T19:01:05Z'),
  )

  expect(instance.runtime.status).toBe('stale')
  expect(instance.runtime.realtime?.snapshot?.instance).toMatchObject({
    version: 'current-release',
    resources: { cpu: { logicalCores: 8 }, memory: { totalBytes: 100 } },
  })
})

test('scope CATCHING_UP keeps server live while exposing only the affected scope as catching up', () => {
  const value = cache([entity('logical_agent', 'logical-1', {
    logical_agent_id: 'logical-1', display_name: 'Oscar', state: 'suspended',
    authority_node_id: 'node-a', authority_epoch: 2, slot_revision: 7,
  })])
  value.scopeStatuses = [{
    sourceNodeId: 'node-a',
    scope: 'commands',
    status: 'CATCHING_UP',
    reason: 'backlog',
    updatedAt: '2026-09-30T19:00:00Z',
  }]
  const [instance] = buildProjectedFleetInstances(
    value,
    [{ sourceNodeId: 'node-a', profile }],
    'live',
  )
  expect(instance.runtime.status).toBe('live')
  expect(instance.runtime.realtime).toMatchObject({ freshness: 'catching_up', catchingUpScopes: ['commands'], staleReason: undefined })
})

test('query task decoder maps stored detail and revision', () => {
  expect(taskFromQueryItem({
    namespace: 'terminal',
    task_id: 'T-9',
    title: 'Cold detail',
    lane: 'implementation',
    priority: 3,
    state: 'done',
    tags: ['M4.5'],
    next_action: '',
    checkpoint: { stage: 'done' },
    result: { ok: true },
    revision: 12,
  })).toMatchObject({
    key: 'terminal/T-9',
    priority: 'P0',
    state: 'done',
    operationalStatus: 'done',
    active: false,
    details: { revision: 12 },
  })
})
