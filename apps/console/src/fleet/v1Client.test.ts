import { expect, test, vi } from 'vitest'
import { FleetV1Client } from './v1Client'

function response(value: object, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

test('fleet v1 client authenticates probe/snapshot/events to exactly one ingress', async () => {
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input))
    expect(init?.headers).toMatchObject({ Authorization: 'Bearer token-a' })
    if (url.pathname.endsWith('/probe')) {
      return response({
        ok: true,
        projection_epoch: 2,
        projection_seq: 4,
        role: 'owner',
        owner_node_id: 'node-a',
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
  expect((await client.snapshot()).fleetId).toBe('fleet-a')
  expect((await client.events(4)).resetRequired).toBe(false)
  expect(fetcher).toHaveBeenCalledTimes(3)
  expect(fetcher.mock.calls.every(([input]) => new URL(String(input)).origin === 'https://node-a.example')).toBe(true)
})

test('fleet v1 client never treats a selector or profile id as authorization', async () => {
  const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    expect(String((init?.headers as Record<string, string>).Authorization)).toBe('Bearer oauth-secret')
    return response({
      ok: true,
      projection_epoch: 1,
      projection_seq: 0,
      role: 'owner',
      owner_node_id: 'ABCD',
      sources: [],
    })
  })
  const client = new FleetV1Client('https://ABCD.example', () => 'oauth-secret', fetcher)
  await client.probe()
})
