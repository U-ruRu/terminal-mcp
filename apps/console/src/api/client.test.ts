import { expect, test, vi } from 'vitest'

import { ConsoleClient, ConsoleHttpError } from './client'
import { ConsoleContractError } from './contracts'

function jsonResponse(body: object, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const task = {
  namespace: 'console',
  task_id: 'M1-003',
  title: 'Typed client',
  lane: 'implementation',
  priority: 'P2',
  state: 'ready',
  operational_status: 'in_progress',
  isolation_hint: 'task/M1-003',
  next_action: 'Finish',
  checkpoint: {},
  tags: ['M1'],
  active: true,
  owner: {
    agent_name: 'Charlie',
    claimed_at: '2026-09-28T09:00:00Z',
    claim_age_seconds: 10,
    claim_intent: 'Implement typed client',
    role: 'owner',
  },
}

const session = {
  name: 'Charlie',
  status: 'active',
  last_activity: '1s ago',
  last_activity_at: '2026-09-28T09:00:01Z',
  intent: 'Implement typed client',
  current_step: 2,
  idle_seconds: 1,
  session_age_seconds: 20,
  work_scope: ['console'],
}

const contexts = {
  primary: [{ id: 1, summary: 'Primary', content: 'Important' }],
  additional: [{ id: 2, summary: 'Extra', content: 'Optional' }],
}

const snapshot = {
  ok: true,
  high_water_seq: 42,
  consistency: {
    mode: 'cursor_first_at_least_once',
    high_water_seq: 42,
    replay_from_seq: 42,
    duplicate_events_possible: true,
  },
  instance: {
    application: 'terminal-mcp',
    version: '0.10.1',
    public_base_url: 'https://terminal.example',
    health: { ok: true, storage: 'ok' },
    resources: {
      status: 'available',
      cpu: {
        status: 'available',
        logical_cores: 4,
        usage_percent: 37.5,
        load_1m: 0.8,
        load_5m: 0.6,
        load_15m: 0.4,
      },
      memory: {
        status: 'available',
        total_bytes: 8589934592,
        used_bytes: 4294967296,
        available_bytes: 4294967296,
        used_percent: 50,
      },
      filesystem: {
        status: 'available',
        total_bytes: 107374182400,
        used_bytes: 53687091200,
        free_bytes: 53687091200,
        used_percent: 50,
      },
      uptime: { status: 'available', seconds: 86400 },
    },
  },
  agents: { sessions: [session] },
  tasks: { summary: { visible: 1 }, tag_counts: { M1: 1 }, tasks: [task] },
  contexts,
  communications: [
    {
      name: 'Charlie',
      messages_awaiting_read: 1,
      messages_awaiting_reply: 0,
      alerts_pending: 0,
      message_journal: [
        {
          message_hash: 'deadbeef',
          state: 'delivered',
          sender_name: 'Mike',
          text: 'Hello',
          require_reply: false,
          alert: false,
          created_at: '2026-09-28T09:00:00Z',
        },
      ],
      intent_journal: [
        {
          timestamp: '2026-09-28T09:00:00Z',
          intent: 'Implement typed client',
          step: 2,
          work_scope: ['console'],
        },
      ],
    },
  ],
}

test('snapshot adapts frozen M0 contract into stable read models', async () => {
  const fetcher = vi.fn(async () => jsonResponse(snapshot))
  const client = new ConsoleClient('https://terminal.example/base', () => 'access-token', fetcher)

  const model = await client.snapshot()

  expect(model).toMatchObject({
    highWaterSeq: 42,
    replayFromSeq: 42,
    duplicateEventsPossible: true,
    instance: {
      application: 'terminal-mcp',
      version: '0.10.1',
      publicBaseUrl: 'https://terminal.example',
      healthy: true,
      resources: {
        status: 'available',
        cpu: { status: 'available', logicalCores: 4, usagePercent: 37.5, load1m: 0.8 },
        memory: { status: 'available', usedPercent: 50 },
        filesystem: { status: 'available', usedPercent: 50 },
        uptime: { status: 'available', seconds: 86400 },
      },
    },
  })
  expect(model.agents[0]).toMatchObject({ name: 'Charlie', currentStep: 2 })
  expect(model.tasks[0]).toMatchObject({
    key: 'console/M1-003',
    operationalStatus: 'in_progress',
    owner: { agentName: 'Charlie' },
  })
  expect(model.contexts).toEqual([
    { id: 1, summary: 'Primary', content: 'Important', primary: true },
    { id: 2, summary: 'Extra', content: 'Optional', primary: false },
  ])
  expect(model.communications[0].messageJournal[0].messageHash).toBe('deadbeef')
  expect(fetcher).toHaveBeenCalledWith(
    new URL('https://terminal.example/actions/console/snapshot'),
    expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer access-token' }),
    }),
  )
})

test('read-only adapters send the M0 request shapes', async () => {
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    void init
    const path = new URL(input.toString()).pathname
    if (path === '/actions/agents') return jsonResponse({ ok: true, sessions: [session] })
    if (path === '/actions/context') return jsonResponse({ ok: true, ...contexts })
    if (path === '/actions/tasks') {
      return jsonResponse({
        ok: true,
        summary: {},
        tag_counts: { M1: 1 },
        tasks: [task],
      })
    }
    if (path === '/console/task-detail') return jsonResponse({ ok: true, task })
    return jsonResponse({ error: 'missing' }, 404)
  })
  const client = new ConsoleClient('https://terminal.example', () => 'token', fetcher)

  expect((await client.agents()).agents).toHaveLength(1)
  expect((await client.tasks()).tasks).toHaveLength(1)
  expect((await client.task('console', 'M1-003')).key).toBe('console/M1-003')
  expect((await client.contexts()).contexts).toHaveLength(2)

  const bodies = fetcher.mock.calls.map(([, init]) =>
    init?.body ? JSON.parse(String(init.body)) : undefined,
  )
  expect(bodies).toContainEqual(
    expect.objectContaining({ show_details: true, since_minutes: 60 }),
  )
  expect(bodies).toContainEqual(
    expect.objectContaining({ show_done: true, show_archived: true, limit: 200 }),
  )
  expect(bodies).toContainEqual({ namespace: 'console', task_id: 'M1-003' })
  expect(bodies).toContainEqual({ action: 'list', show_details: true })
})

test('websocket ticket uses paired access auth and validates ticket contract', async () => {
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    expect(new URL(input.toString()).pathname).toBe('/console/ws-ticket')
    expect(init?.method).toBe('POST')
    return jsonResponse({ ticket: 'one-time-ws-ticket', expires_in: 30 })
  })
  const client = new ConsoleClient('https://terminal.example', () => 'paired-access', fetcher)

  await expect(client.webSocketTicket()).resolves.toEqual({
    ticket: 'one-time-ws-ticket',
    expiresIn: 30,
  })
  expect(fetcher).toHaveBeenCalledWith(
    new URL('https://terminal.example/console/ws-ticket'),
    expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ Authorization: 'Bearer paired-access' }),
    }),
  )
})

test('HTTP errors remain distinct from successful-response contract errors', async () => {
  const unauthorized = new ConsoleClient(
    'https://terminal.example',
    () => 'bad',
    async () => jsonResponse({ error: 'invalid_token' }, 401),
  )
  await expect(unauthorized.snapshot()).rejects.toMatchObject({
    name: 'ConsoleHttpError',
    status: 401,
    code: 'invalid_token',
    authenticationRequired: true,
  } satisfies Partial<ConsoleHttpError>)

  const malformed = new ConsoleClient(
    'https://terminal.example',
    () => 'token',
    async () => jsonResponse({ ...snapshot, high_water_seq: 'forty-two' }),
  )
  await expect(malformed.snapshot()).rejects.toBeInstanceOf(ConsoleContractError)
})

test('network failures are retryable HTTP errors', async () => {
  const client = new ConsoleClient('https://terminal.example', () => 'token', async () => {
    throw new TypeError('offline')
  })

  try {
    await client.snapshot()
    throw new Error('expected request to fail')
  } catch (error) {
    expect(error).toBeInstanceOf(ConsoleHttpError)
    const httpError = error as ConsoleHttpError
    expect(httpError.status).toBe(0)
    expect(httpError.retryable).toBe(true)
  }
})


test('activity feed maps cursor, filters and authorized message projection', async () => {
  const response = {
    ok: true,
    events: [
      {
        seq: 7,
        event_type: 'message.created',
        entity_type: 'message',
        entity_id: 'deadbeef',
        actor_name: 'Alpha',
        payload: { alert: true },
        created_at: '2026-09-28T11:00:00Z',
        message: {
          message_hash: 'deadbeef',
          sender_name: 'Alpha',
          target: 'Bravo',
          text: 'Operational hello',
          require_reply: true,
          alert: true,
          task_namespace: 'console',
          task_id: 'M2-008',
          recipients: [{ name: 'Bravo', seen: true, read: false, replied: false }],
        },
      },
    ],
    since: 4,
    next_cursor: 7,
    oldest_seq: 1,
    high_water_seq: 9,
    gap: false,
    gap_from_seq: null,
    gap_to_seq: null,
  }
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    expect(new URL(input.toString()).pathname).toBe('/console/activity')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({
      since: 4,
      limit: 20,
      event_types: ['message.created'],
      entity_types: ['message'],
    })
    return jsonResponse(response)
  })
  const client = new ConsoleClient('https://terminal.example', () => 'paired-access', fetcher)

  const model = await client.activity({
    since: 4,
    limit: 20,
    eventTypes: ['message.created'],
    entityTypes: ['message'],
  })

  expect(model).toMatchObject({ since: 4, nextCursor: 7, highWaterSeq: 9, gap: false })
  expect(model.events[0]).toMatchObject({
    seq: 7,
    eventType: 'message.created',
    entityType: 'message',
    actorName: 'Alpha',
    message: {
      text: 'Operational hello',
      recipients: [{ name: 'Bravo', seen: true, read: false, replied: false }],
    },
  })
})
