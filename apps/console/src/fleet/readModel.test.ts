import { expect,test } from 'vitest'

import type { AgentReadModel,ConsoleSnapshotReadModel,TaskReadModel } from '../api/models'
import type { ConnectionProfile } from '../connections/types'
import type { RealtimeState } from '../realtime/state'
import { buildFleetReadModel } from './readModel'
import type { FleetInstanceView } from './types'

function snapshot(
  origin: string,
  version: string,
  options: {
    agentName?: string
    intent?: string
    sessionRef?: string
    originInstanceId?: string
    sessionAgeSeconds?: number
    sessionRemainingSeconds?: number
    attachment?: boolean
    intentScopes?: AgentReadModel['intentScopes']
    localIntentStatus?: AgentReadModel['localIntentStatus']
    tasks?: TaskReadModel[]
    alert?: number
    reply?: number
    unread?: number
    messageAt?: string
  } = {},
): ConsoleSnapshotReadModel {
  const agentName = options.agentName
  return {
    highWaterSeq: 12,
    replayFromSeq: 12,
    duplicateEventsPossible: true,
    instance: {
      application: 'terminal-mcp',
      version,
      publicBaseUrl: origin,
      healthy: true,
      health: { service: 'ok' },
      resources: {
        status: 'available',
        cpu: { status: 'available', logicalCores: 4, usagePercent: 37.5, load1m: 0.8, load5m: 0.6, load15m: 0.4 },
        memory: { status: 'available', totalBytes: 8589934592, usedBytes: 4294967296, availableBytes: 4294967296, usedPercent: 50 },
        filesystem: { status: 'available', totalBytes: 107374182400, usedBytes: 53687091200, freeBytes: 53687091200, usedPercent: 50 },
        uptime: { status: 'available', seconds: 86400 },
      },
    },
    agents: agentName
      ? [
          {
            name: agentName,
            status: 'active',
            intent: options.intent ?? 'working',
            currentStep: 2,
            lastActivity: '5s ago',
            lastActivityAt: '2026-09-28T10:02:00Z',
            sessionRef: options.sessionRef,
            originInstanceId: options.originInstanceId,
            sessionStartedAt: '2026-09-28T10:00:00Z',
            sessionAgeSeconds: options.sessionAgeSeconds,
            sessionRemainingSeconds: options.sessionRemainingSeconds,
            attachment: options.attachment,
            intentScopes: options.intentScopes,
            localIntentStatus: options.localIntentStatus,
            workScope: [],
            messagesAwaitingRead: 0,
            messagesAwaitingReply: 0,
            alertsPending: 0,
          },
        ]
      : [],
    tasks: options.tasks ?? [],
    contexts: [],
    communications: [
      {
        name: agentName ?? 'operator',
        messagesAwaitingRead: options.unread ?? 0,
        messagesAwaitingReply: options.reply ?? 0,
        alertsPending: options.alert ?? 0,
        messageJournal: options.messageAt
          ? [
              {
                messageHash: 'msg-' + version,
                state: 'seen',
                senderName: 'Coordinator',
                text: 'status ' + version,
                requireReply: (options.reply ?? 0) > 0,
                alert: (options.alert ?? 0) > 0,
                createdAt: options.messageAt,
              },
            ]
          : [],
        intentJournal: [],
      },
    ],
  }
}

function profile(instanceId: string, displayName: string): ConnectionProfile {
  return {
    instanceId,
    origin: 'https://' + instanceId + '.example',
    displayName,
    credentialRef: 'credential-' + instanceId,
    metadata: {
      deviceId: 'device-' + instanceId,
      clientId: 'client-' + instanceId,
      deviceLabel: displayName,
      scope: 'terminal:read',
      pairedAt: 1,
    },
    createdAt: 1,
    updatedAt: 1,
  }
}

function realtime(
  status: RealtimeState['status'],
  value: ConsoleSnapshotReadModel | null,
  options: {
    lastEventAt?: string
    lastError?: string
    staleReason?: string
    reconnectAttempt?: number
    statusSince?: string
    freshnessSince?: string
  } = {},
): RealtimeState {
  return {
    status,
    snapshot: value,
    cursor: value?.replayFromSeq ?? 0,
    highWaterSeq: value?.highWaterSeq ?? 0,
    socketConnected: status === 'live',
    reconnectAttempt: options.reconnectAttempt ?? 0,
    freshness: status === 'live' ? 'fresh' : 'stale',
    catchingUpScopes: [],
    lastError: options.lastError,
    staleReason: options.staleReason,
    statusSince: options.statusSince,
    freshnessSince: options.freshnessSince,
    lastEvent: options.lastEventAt
      ? {
          seq: 13,
          eventType: 'task.updated',
          entityType: 'task',
          entityId: 'M2-003',
          payload: {},
          createdAt: options.lastEventAt,
        }
      : undefined,
  }
}

function instance(
  instanceId: string,
  displayName: string,
  status: RealtimeState['status'],
  value: ConsoleSnapshotReadModel | null,
  options: Parameters<typeof realtime>[2] = {},
): FleetInstanceView {
  return {
    profile: profile(instanceId, displayName),
    runtime: {
      instanceId,
      status,
      authStatus: value ? 'connected' : 'unpaired',
      realtime: realtime(status, value, options),
      reconnectAttempt: options.reconnectAttempt ?? 0,
      lastError: options.lastError,
      statusSince: options.statusSince,
    },
  }
}

test('orders servers by their current connection problem', () => {
  const live = instance('live', 'Zulu Live', 'live', snapshot('https://live.example', 'current-release'))
  const stale = instance('stale', 'Bravo Stale', 'stale', snapshot('https://stale.example', 'current-release'))
  const offline = instance('offline', 'Alpha Offline', 'offline', null)

  const model = buildFleetReadModel([live, offline, stale])

  expect(model.servers.map((server) => server.instanceId)).toEqual(['offline', 'stale', 'live'])
})

test('keeps warning and draining sessions current while excluding cooldown and ended sessions', () => {
  const value = snapshot('https://node.example', 'current-release', { agentName: 'Agent' })
  value.agents = ['active', 'warning', 'draining', 'cooldown', 'ended'].map((status) => ({
    ...value.agents[0],
    name: status,
    status,
    intent: 'intent-' + status,
    sessionRef: 'session-' + status,
  }))

  const model = buildFleetReadModel([instance('node', 'Node', 'live', value)])

  expect(model.activeAgents.map(({ agent }) => agent.status)).toEqual(['active', 'draining', 'warning'])
  expect(model.sessions.map((session) => session.sessionRef)).toEqual(['session-active', 'session-draining', 'session-warning'])
  expect(model.summary.activeAgents).toBe(3)
  expect(model.servers[0].activeAgentCount).toBe(3)
  expect(model.servers[0].activeIntents).toEqual(['intent-active', 'intent-draining', 'intent-warning'])
})

test('collapses one shared session across origin and attachment while preserving the global budget', () => {
  const origin = instance(
    'origin',
    'Origin',
    'live',
    snapshot('https://origin.example', '0.10.1', {
      agentName: 'Alpha',
      intent: 'origin work',
      sessionRef: 'session-shared',
      originInstanceId: 'origin',
      sessionAgeSeconds: 900,
      sessionRemainingSeconds: 480,
    }),
  )
  const attachment = instance(
    'peer',
    'Peer',
    'live',
    snapshot('https://peer.example', '0.10.1', {
      agentName: 'Alpha',
      intent: 'continued on peer',
      sessionRef: 'session-shared',
      originInstanceId: 'origin',
      sessionAgeSeconds: 905,
      sessionRemainingSeconds: 475,
      attachment: true,
    }),
  )

  const model = buildFleetReadModel([attachment, origin])

  expect(model.sessions).toHaveLength(1)
  expect(model.sessions[0]).toMatchObject({
    sessionRef: 'session-shared',
    name: 'Alpha',
    originInstanceId: 'origin',
    sessionAgeSeconds: 905,
    sessionRemainingSeconds: 475,
  })
  expect(model.sessions[0].attachments.map((item) => item.instanceId)).toEqual(['origin', 'peer'])
})

test('keeps server-scoped intents distinct and never relabels remote intent as local', () => {
  const scopes = [
    {
      instanceId: 'origin',
      intent: 'origin work',
      currentStep: 1,
      updatedAt: '2026-09-28T10:02:00Z',
      ageSeconds: 5,
      status: 'fresh' as const,
      local: true,
    },
    {
      instanceId: 'peer',
      intent: 'peer work',
      currentStep: 2,
      updatedAt: '2026-09-28T09:50:00Z',
      ageSeconds: 720,
      status: 'stale' as const,
      local: false,
    },
  ]
  const origin = instance(
    'origin',
    'Origin',
    'live',
    snapshot('https://origin.example', '0.10.1', {
      agentName: 'Alpha',
      intent: 'origin work',
      sessionRef: 'session-scoped',
      originInstanceId: 'origin',
      intentScopes: scopes,
      localIntentStatus: 'fresh',
    }),
  )
  const peer = instance(
    'peer',
    'Peer',
    'live',
    snapshot('https://peer.example', '0.10.1', {
      agentName: 'Alpha',
      intent: 'origin work',
      sessionRef: 'session-scoped',
      originInstanceId: 'origin',
      attachment: true,
      intentScopes: scopes.map((scope) => ({
        ...scope,
        local: scope.instanceId === 'peer',
      })),
      localIntentStatus: 'stale',
    }),
  )

  const model = buildFleetReadModel([peer, origin])
  expect(model.sessions).toHaveLength(1)
  expect(model.sessions[0].scopedIntents).toEqual([
    expect.objectContaining({ instanceId: 'origin', displayName: 'Origin', intent: 'origin work', status: 'fresh' }),
    expect.objectContaining({ instanceId: 'peer', displayName: 'Peer', intent: 'peer work', status: 'stale' }),
  ])
  expect(model.servers.find((server) => server.instanceId === 'origin')?.activeIntents).toEqual([
    'origin work',
  ])
  expect(model.servers.find((server) => server.instanceId === 'peer')?.activeIntents).toEqual([])
})

test.each([
  { status: 'offline' as const, expectedSince: '2026-10-03T12:03:00Z' },
  { status: 'stale' as const, expectedSince: '2026-10-03T12:05:00Z' },
])('projects the current $status transition timestamp', ({ status, expectedSince }) => {
  const value = instance('node-a', 'Node A', status, snapshot('https://node-a.example', 'current-release'), {
    statusSince: '2026-10-03T12:03:00Z',
    freshnessSince: '2026-10-03T12:05:00Z',
    lastEventAt: '2026-10-03T10:00:00Z',
  })

  const model = buildFleetReadModel([value])

  expect(model.servers[0].stateSince).toBe(expectedSince)
})
