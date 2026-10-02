import { expect, test } from 'vitest'
import type { ActivityEventReadModel } from '../api/models'
import { projectActivity, renderActivity } from './chatProjection'

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

test('groups same actor within 120 seconds and inserts one local date separator', () => {
  const items = projectActivity([
    event(1),
    event(2, { payload: { state: 'stopping' } }),
    event(3, { actorId: 'la-b', actorName: 'Bravo', entityId: 'agent-b' }),
  ], 'en', 'Main')
  const rendered = renderActivity(items, 'en')
  expect(rendered.filter((item) => item.kind === 'date')).toHaveLength(1)
  const messages = rendered.filter((item) => item.kind === 'message')
  expect(messages.map((item) => item.kind === 'date' ? false : item.showIdentity)).toEqual([true, false, true])
  expect(messages.map((item) => item.kind === 'date' ? '' : item.content)).toEqual(['Started session', 'Ending session', 'Started session'])
})

test('collapses command lifecycle into one logical item with latest status', () => {
  const items = projectActivity([
    event(1, { eventType: 'command.created', entityType: 'command', entityId: 'cmd-1', payload: { command: 'uptime', status: 'queued' } }),
    event(2, { eventType: 'command.status', entityType: 'command', entityId: 'cmd-1', payload: { status: 'running' } }),
    event(3, { eventType: 'command.status', entityType: 'command', entityId: 'cmd-1', payload: { status: 'completed', duration: '2s' } }),
  ], 'en', 'Main')
  expect(items).toHaveLength(1)
  expect(items[0].content).toBe('$ uptime')
  expect(items[0].commands).toHaveLength(1)
  expect(items[0].commands?.[0]).toMatchObject({ label: '$ uptime', status: '✓ Completed · 2s' })
  expect(items[0].events).toHaveLength(3)
})

test('projects service health without raw backend event name', () => {
  const items = projectActivity([
    event(1, { eventType: 'health.changed', entityType: 'health', entityId: 'terminal-mcp', actorId: undefined, actorName: undefined, payload: { ok: false, status: 'offline' } }),
  ], 'en', 'Firstbyte')
  expect(items).toHaveLength(1)
  expect(items[0]).toMatchObject({ kind: 'service', tone: 'critical', content: 'Firstbyte lost connection' })
})

test('batches consecutive commands by the same actor without exposing command hashes', () => {
  const items = projectActivity([
    event(1, { eventType: 'command.created', entityType: 'command', entityId: 'hash-a', payload: { status: 'completed' } }),
    event(2, { eventType: 'command.created', entityType: 'command', entityId: 'hash-b', payload: { command: 'uptime', status: 'completed' } }),
    event(3, { eventType: 'command.created', entityType: 'command', entityId: 'hash-c', actorId: 'la-b', actorName: 'Bravo', payload: { command: 'whoami', status: 'completed' } }),
  ], 'ru', 'Main')
  expect(items).toHaveLength(2)
  expect(items[0].content).toBe('Вызвал 2 команды')
  expect(items[0].commands).toHaveLength(2)
  expect(items[0].commands?.[0].label).toBe('Команда')
  expect(items[0].commands?.[1].label).toBe('$ uptime')
  expect(items[0].content).not.toContain('hash-a')
  expect(items[1].content).toBe('$ whoami')
})

test('resolves command attribution logical agent to its public display name', () => {
  const items = projectActivity([
    event(1, { eventType: 'command.created', entityType: 'command', entityId: 'cmd-attributed', actorId: undefined, actorName: undefined, payload: { command: 'date', status: 'queued' } }),
    event(2, { eventType: 'command.attribution', entityType: 'command', entityId: 'cmd-attributed', actorId: undefined, actorName: undefined, payload: { logical_agent_id: 'la-real', status: 'running' } }),
  ], 'en', 'Main', { 'la-real': 'Operator' })
  expect(items).toHaveLength(1)
  expect(items[0]).toMatchObject({ actorId: 'la-real', actorName: 'Operator', content: '$ date' })
})

test('assigns stable distinct anonymous identities and renders unattributed events as service rows', () => {
  const items = projectActivity([
    event(1, { actorId: 'la-private-a', actorName: undefined }),
    event(2, { actorId: 'la-private-a', actorName: undefined, payload: { state: 'stopping' } }),
    event(3, { actorId: 'la-private-b', actorName: undefined, entityId: 'agent-b' }),
    event(4, { eventType: 'runtime.changed', entityType: 'runtime', entityId: 'runtime', actorId: undefined, actorName: undefined, payload: { summary: 'Runtime updated' } }),
  ], 'ru', 'Main')
  expect(items[0].actorName).toMatch(/^Агент · [A-Z0-9]{4}$/)
  expect(items[1].actorName).toBe(items[0].actorName)
  expect(items[2].actorName).toMatch(/^Агент · [A-Z0-9]{4}$/)
  expect(items[2].actorName).not.toBe(items[0].actorName)
  expect(items[3]).toMatchObject({ kind: 'service', content: 'Main: Runtime updated' })
})

test('summarizes a multi-command batch without numbered placeholder rows', () => {
  const items = projectActivity([
    event(1, { eventType: 'command.created', entityType: 'command', entityId: 'cmd-a', payload: { command_type: 'persistent_run', status: 'completed' } }),
    event(2, { eventType: 'command.created', entityType: 'command', entityId: 'cmd-b', payload: { command_type: 'read', status: 'cancelled' } }),
  ], 'ru', 'Main')
  expect(items).toHaveLength(1)
  expect(items[0]).toMatchObject({ content: 'Вызвал 2 команды' })
  expect(items[0].secondary).toContain('1 завершена')
  expect(items[0].secondary).toContain('1 отменена')
  expect(items[0].commands?.map((command) => command.label)).toEqual(['persistent run', 'read'])
})

test('renders an unattributed command as a service operation instead of Unknown agent', () => {
  const items = projectActivity([
    event(1, { eventType: 'command.created', entityType: 'command', entityId: 'cmd-system', actorId: undefined, actorName: undefined, payload: { command_type: 'persistent_run', status: 'running' } }),
  ], 'en', 'Secondary')
  expect(items[0]).toMatchObject({ kind: 'service', content: 'Secondary: persistent run · Running…' })
  expect(items[0].actorName).toBeUndefined()
})
