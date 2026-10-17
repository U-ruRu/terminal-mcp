import { expect,test } from 'vitest'
import type { ActivityEventReadModel } from '../api/models'
import { projectActivity } from './chatProjection'

function event(seq: number, overrides: Partial<ActivityEventReadModel> = {}): ActivityEventReadModel {
  return {
    seq,
    eventType: 'logical_agent.changed',
    entityType: 'agent',
    entityId: 'agent-a',
    actorId: 'la-a',
    actorName: 'Alpha',
    payload: { state: 'active' },
    createdAt: '2026-10-02T10:00:' + String(seq).padStart(2, '0') + 'Z',
    ...overrides,
  }
}

test('collapses command lifecycle into one logical item with latest status', () => {
  const items = projectActivity([
    event(1, { eventType: 'command.created', entityType: 'command', entityId: 'cmd-1', payload: { command: 'uptime', status: 'queued' } }),
    event(2, { eventType: 'command.status', entityType: 'command', entityId: 'cmd-1', payload: { status: 'running' } }),
    event(3, { eventType: 'command.status', entityType: 'command', entityId: 'cmd-1', payload: { status: 'completed', duration: '2s' } }),
  ], 'en', 'Main')
  expect(items).toHaveLength(1)
  expect(items[0].content).toBe('$ uptime')
  expect(items[0].commands).toHaveLength(1)
  expect(items[0].commands?.[0]).toMatchObject({ label: '$ uptime', status: 'Completed · 2s' })
  expect(items[0].events).toHaveLength(3)
})

test('projects service health without raw backend event name', () => {
  const items = projectActivity([
    event(1, { eventType: 'health.changed', entityType: 'health', entityId: 'terminal-mcp', actorId: undefined, actorName: undefined, payload: { ok: false, status: 'offline' } }),
  ], 'en', 'Firstbyte')
  expect(items).toHaveLength(1)
  expect(items[0]).toMatchObject({ kind: 'service', tone: 'critical', content: 'Firstbyte lost connection' })
})

test('resolves command attribution logical agent to its public display name', () => {
  const items = projectActivity([
    event(1, { eventType: 'command.created', entityType: 'command', entityId: 'cmd-attributed', actorId: undefined, actorName: undefined, payload: { command: 'date', status: 'queued' } }),
    event(2, { eventType: 'command.attribution', entityType: 'command', entityId: 'cmd-attributed', actorId: undefined, actorName: undefined, payload: { logical_agent_id: 'la-real', status: 'running' } }),
  ], 'en', 'Main', { 'la-real': 'Operator' })
  expect(items).toHaveLength(1)
  expect(items[0]).toMatchObject({ actorId: 'la-real', actorName: 'Operator', content: '$ date' })
})

test('deduplicates message mirrors by message hash and resolves internal recipient identity', () => {
  const message = {
    messageHash: 'msg-1',
    senderAgentId: 'la-oscar',
    senderName: 'la-oscar',
    target: 'la-coordinator',
    text: 'Hello once',
    requireReply: false,
    alert: false,
    recipients: [{ agentId: 'la-coordinator', name: 'la-coordinator', seen: true, read: false, replied: false }],
  }
  const items = projectActivity([
    event(1, { eventType: 'message.created', entityType: 'message', entityId: 'msg-1', actorId: 'la-oscar', actorName: 'la-oscar', message }),
    event(2, { eventType: 'message.receipt', entityType: 'message', entityId: 'msg-1', actorId: 'la-coordinator', actorName: undefined, message }),
  ], 'ru', 'Tokyo', { 'la-oscar': 'Oscar', 'la-coordinator': 'Coordinator' })
  expect(items).toHaveLength(1)
  expect(items[0]).toMatchObject({ key: 'message:msg-1', actorName: 'Oscar', content: 'Hello once', secondary: '→ Coordinator' })
  expect(items[0].events).toHaveLength(2)
})

test('coalesces mirrored session lifecycle transitions within two seconds', () => {
  const items = projectActivity([
    event(1, { eventType: 'logical_agent.changed', entityType: 'logical_agent', entityId: 'la-a', actorId: undefined, actorName: undefined, payload: { display_name: 'Alpha', state: 'armed' }, createdAt: '2026-10-02T10:00:00.000Z' }),
    event(2, { eventType: 'work_session.created', entityType: 'work_session', entityId: 'ws-a', actorId: undefined, actorName: undefined, payload: { logical_agent_id: 'la-a', state: 'active' }, createdAt: '2026-10-02T10:00:01.000Z' }),
    event(3, { eventType: 'logical_agent.changed', entityType: 'logical_agent', entityId: 'la-a', actorId: undefined, actorName: undefined, payload: { display_name: 'Alpha', state: 'active' }, createdAt: '2026-10-02T10:00:01.100Z' }),
  ], 'ru', 'Main', { 'la-a': 'Alpha' })
  expect(items).toHaveLength(1)
  expect(items[0]).toMatchObject({ actorName: 'Alpha', content: 'Начал сессию' })
  expect(items[0].events).toHaveLength(3)
})
