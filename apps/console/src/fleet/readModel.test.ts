import { expect, test } from 'vitest'

import type { ConsoleSnapshotReadModel, TaskReadModel } from '../api/models'
import type { ConnectionProfile } from '../connections/types'
import type { RealtimeState } from '../realtime/state'
import { buildFleetReadModel } from './readModel'
import type { FleetInstanceView } from './types'

function task(
  taskId: string,
  operationalStatus: TaskReadModel['operationalStatus'],
  priority: TaskReadModel['priority'] = 'P2',
  archived = false,
): TaskReadModel {
  return {
    key: 'console/' + taskId,
    namespace: 'console',
    taskId,
    title: 'Task ' + taskId,
    lane: 'implementation',
    priority,
    state: operationalStatus === 'in_progress' ? 'ready' : operationalStatus,
    operationalStatus,
    active: operationalStatus === 'in_progress',
    archived,
    tags: [],
    nextAction: 'next ' + taskId,
    checkpoint: {},
  }
}

function snapshot(
  origin: string,
  version: string,
  options: {
    agentName?: string
    intent?: string
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
  } = {},
): RealtimeState {
  return {
    status,
    snapshot: value,
    cursor: value?.replayFromSeq ?? 0,
    highWaterSeq: value?.highWaterSeq ?? 0,
    socketConnected: status === 'live',
    reconnectAttempt: options.reconnectAttempt ?? 0,
    lastError: options.lastError,
    staleReason: options.staleReason,
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
    },
  }
}

test('aggregates mixed live, stale and offline servers with problem-first deterministic ordering', () => {
  const live = instance(
    'live',
    'Zulu Live',
    'live',
    snapshot('https://live.example', '0.10.1', {
      agentName: 'Alpha',
      intent: 'ship dashboard',
      tasks: [task('READY', 'ready'), task('BLOCK', 'blocked', 'P1')],
      alert: 1,
      reply: 2,
      unread: 3,
      messageAt: '2026-09-28T10:04:00Z',
    }),
    { lastEventAt: '2026-09-28T10:05:00Z' },
  )
  const stale = instance(
    'stale',
    'Bravo Stale',
    'stale',
    snapshot('https://stale.example', '0.10.0', {
      agentName: 'Beta',
      tasks: [task('RUN', 'in_progress', 'P0')],
    }),
    { staleReason: 'cursor_gap', reconnectAttempt: 2 },
  )
  const offline = instance('offline', 'Alpha Offline', 'offline', null, {
    lastError: 'network_error',
    reconnectAttempt: 4,
  })

  const model = buildFleetReadModel([live, offline, stale])

  expect(model.servers.map((server) => server.instanceId)).toEqual([
    'offline',
    'stale',
    'live',
  ])
  expect(model.servers.map((server) => server.freshness)).toEqual([
    'offline',
    'stale',
    'fresh',
  ])
  expect(model.summary).toEqual({
    totalServers: 3,
    liveServers: 1,
    staleServers: 1,
    offlineServers: 1,
    activeAgents: 2,
    blockedTasks: 1,
    replyRequired: 2,
    alerts: 1,
  })

  const liveServer = model.servers.find((server) => server.instanceId === 'live')
  expect(liveServer).toMatchObject({
    version: '0.10.1',
    activeAgentCount: 1,
    activeIntents: ['ship dashboard'],
    taskCounts: {
      ready: 1,
      inProgress: 0,
      blocked: 1,
      deferred: 0,
      done: 0,
      highPriorityOpen: 1,
    },
    communication: { unread: 3, replyRequired: 2, alerts: 1 },
    lastSeenAt: '2026-09-28T10:05:00Z',
  })
})

test('preserves source server identity for agents, blockers and activity', () => {
  const one = instance(
    'one',
    'One',
    'live',
    snapshot('https://one.example', 'one', {
      agentName: 'SameName',
      tasks: [task('BLOCK-ONE', 'blocked')],
      messageAt: '2026-09-28T10:03:00Z',
    }),
    { lastEventAt: '2026-09-28T10:06:00Z' },
  )
  const two = instance(
    'two',
    'Two',
    'live',
    snapshot('https://two.example', 'two', {
      agentName: 'SameName',
      tasks: [task('BLOCK-TWO', 'blocked')],
      messageAt: '2026-09-28T10:04:00Z',
    }),
    { lastEventAt: '2026-09-28T10:05:00Z' },
  )

  const model = buildFleetReadModel([two, one])

  expect(model.activeAgents.map((item) => item.instanceId)).toEqual(['one', 'two'])
  expect(model.blockers.map((item) => item.instanceId + '/' + item.task.taskId)).toEqual([
    'one/BLOCK-ONE',
    'two/BLOCK-TWO',
  ])
  expect(model.recentActivity.map((item) => item.instanceId + ':' + item.kind)).toEqual([
    'one:event',
    'two:event',
    'two:message',
    'one:message',
  ])
})

test('offline server keeps last-known snapshot but is never classified as fresh', () => {
  const remembered = instance(
    'remembered',
    'Remembered',
    'offline',
    snapshot('https://remembered.example', '0.9.9', {
      agentName: 'OldAgent',
      tasks: [task('OLD-BLOCK', 'blocked')],
    }),
    { lastError: 'socket_closed' },
  )

  const model = buildFleetReadModel([remembered])
  expect(model.servers[0]).toMatchObject({
    connectivity: 'offline',
    freshness: 'offline',
    version: '0.9.9',
    snapshotAvailable: true,
    activeAgentCount: 1,
    blockerCount: 1,
    lastError: 'socket_closed',
  })
  expect(model.summary.offlineServers).toBe(1)
})

test('ignores archived task pressure and applies a deterministic activity limit', () => {
  const value = snapshot('https://alpha.example', '0.10.1', {
    tasks: [
      task('ARCHIVED', 'blocked', 'P0', true),
      task('ACTIVE', 'blocked', 'P2'),
    ],
    messageAt: '2026-09-28T10:04:00Z',
  })
  value.communications[0].messageJournal.push({
    messageHash: 'older',
    state: 'read',
    senderName: 'Coordinator',
    text: 'older',
    requireReply: false,
    alert: false,
    createdAt: '2026-09-28T10:01:00Z',
  })
  const model = buildFleetReadModel(
    [
      instance('alpha', 'Alpha', 'live', value, {
        lastEventAt: '2026-09-28T10:05:00Z',
      }),
    ],
    2,
  )

  expect(model.summary.blockedTasks).toBe(1)
  expect(model.servers[0].taskCounts.blocked).toBe(1)
  expect(model.servers[0].taskCounts.highPriorityOpen).toBe(0)
  expect(model.recentActivity).toHaveLength(2)
  expect(model.recentActivity.map((item) => item.occurredAt)).toEqual([
    '2026-09-28T10:05:00Z',
    '2026-09-28T10:04:00Z',
  ])
})
