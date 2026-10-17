import { expect,test,vi } from 'vitest'
import { FleetV1Client } from './v1Client'

function response(value: object, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

test('current Fleet probe authenticates to the selected ingress', async () => {
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input))
    expect(init?.headers).toMatchObject({ Authorization: 'Bearer token-a' })
    if (url.pathname.endsWith('/probe')) {
      return response({
        ok: true,
        protocol_major: 1,
        fleet_id: 'fleet-a',
        node_id: 'node-a',
        projection_epoch: 2,
        projection_seq: 4,
        role: 'owner',
        owner_node_id: 'node-a',
        capabilities: ['fleet.projection.current-v2'],
        sources: [],
      })
    }
    if (url.pathname.endsWith('/snapshot')) {
      return response({
        fleet_id: 'fleet-a',
        node_id: 'node-a',
        owner_node_id: 'node-a',
        role: 'owner',
        projection_epoch: 2,
        projection_seq: 4,
        updated_at: 'now',
        sources: [],
        entities: [],
        runtime_overlays: [],
      })
    }
    return response({
      projection_epoch: 2,
      projection_seq: 4,
      oldest_projection_seq: 1,
      newest_projection_seq: 4,
      reset_required: false,
      events: [],
    })
  })
  const client = new FleetV1Client('https://node-a.example', () => 'token-a', fetcher)
  expect((await client.probe()).projectionSeq).toBe(4)
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(fetcher.mock.calls.every(([input]) => new URL(String(input)).origin === 'https://node-a.example')).toBe(true)
})

test('fleet v1 query plane preserves source provenance and partial completeness', async () => {
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input))
    expect((init?.headers as Record<string, string>).Authorization).toBe('Bearer token-a')
    if (url.pathname.endsWith('/query/tasks')) {
      expect(init?.method).toBe('POST')
      expect(JSON.parse(String(init?.body))).toMatchObject({
        source_node_ids: ['source-a'],
        q: 'M45',
        include_count: true,
      })
      return response({
        operation: 'query',
        resource: 'tasks',
        sources: [
          { source_node_id: 'source-a', ok: true, status: 'LIVE', data: { items: [{ task_id: 'T-1' }] } },
          { source_node_id: 'source-b', ok: false, status: 'DEGRADED', error: 'TimeoutError' },
        ],
        partial: true,
        complete: false,
      })
    }
    if (url.pathname.endsWith('/detail/tasks')) {
      expect(url.searchParams.get('entity_id')).toBe('terminal/T-1')
      expect(url.searchParams.get('source_node_id')).toBe('source-a')
      return response({
        operation: 'detail',
        resource: 'tasks',
        sources: [{ source_node_id: 'source-a', ok: true, status: 'LIVE', data: { task_id: 'T-1' } }],
        partial: false,
        complete: true,
      })
    }
    return response({ error: 'missing' }, 404)
  })
  const client = new FleetV1Client('https://node-a.example', () => 'token-a', fetcher)
  const queried = await client.query<{ items: Array<{ task_id: string }> }>('tasks', {
    sourceNodeIds: ['source-a'],
    q: 'M45',
    includeCount: true,
  })
  expect(queried.partial).toBe(true)
  expect(queried.sources[0].sourceNodeId).toBe('source-a')
  expect(queried.sources[1].status).toBe('DEGRADED')

})
