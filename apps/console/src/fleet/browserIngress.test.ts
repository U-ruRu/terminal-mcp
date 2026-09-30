import { expect, test, vi } from 'vitest'
import type { ConnectionProfile, ProfileRestoreResult } from '../connections/types'
import { BrowserFleetIngressEndpoint } from './browserIngress'

function profile(): ConnectionProfile {
  return {
    instanceId: 'profile-a',
    origin: 'https://node-a.example',
    displayName: 'Node A',
    credentialRef: 'credential-a',
    metadata: { deviceId: 'device-a', clientId: 'client-a', deviceLabel: 'Console', scope: 'terminal:read', pairedAt: 1 },
    createdAt: 1,
    updatedAt: 1,
  }
}
function connected(): ProfileRestoreResult {
  return { status: 'connected', profile: profile(), accessToken: 'oauth-token', accessExpiresAt: 100_000 }
}
function json(value: object, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
}

test('candidate authenticates only to its own origin and derives quality from Fleet probe', async () => {
  const credentials = { restore: vi.fn(async () => connected()) }
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    expect(new URL(String(input)).origin).toBe('https://node-a.example')
    expect((init?.headers as Record<string, string>).Authorization).toBe('Bearer oauth-token')
    return json({
      ok: true,
      protocol_major: 1,
      fleet_id: 'fleet-a',
      node_id: 'node-a',
      projection_epoch: 2,
      projection_seq: 9,
      role: 'owner',
      owner_node_id: 'node-a',
      capabilities: ['fleet.projection.current-v2', 'fleet.projection.query-relay-v2'],
      sources: [
        { source_node_id: 'node-a', source_stream_generation: 'g1', source_seq: 4, freshness: 'fresh', updated_at: 'now' },
        { source_node_id: 'node-b', source_stream_generation: 'g2', source_seq: 8, freshness: 'stale', updated_at: 'now' },
      ],
    })
  })
  let clock = 1000
  const endpoint = new BrowserFleetIngressEndpoint(profile(), credentials, fetcher, () => { clock += 25; return clock })
  const probe = await endpoint.probe()
  expect(probe).toMatchObject({
    candidateId: 'profile-a', authenticated: true, compatible: true, fleetId: 'fleet-a',
    projectionEpoch: 2, projectionSeq: 9, completeness: 1, freshness: 0.5,
  })
  expect(endpoint.nodeId).toBe('node-a')
  expect(credentials.restore).toHaveBeenCalledWith('profile-a')
})

test('missing OAuth credential fails closed before network use', async () => {
  const revoked: ProfileRestoreResult = { status: 'revoked', profile: profile() }
  const credentials = { restore: vi.fn(async () => revoked) }
  const fetcher = vi.fn()
  const endpoint = new BrowserFleetIngressEndpoint(profile(), credentials, fetcher)
  await expect(endpoint.probe()).rejects.toThrow('fleet_ingress_auth_revoked')
  expect(fetcher).not.toHaveBeenCalled()
})

test('candidate without current projection capability is not compatible', async () => {
  const credentials = { restore: vi.fn(async () => connected()) }
  const fetcher = vi.fn(async () => json({
    ok: true, protocol_major: 1, fleet_id: 'fleet-a', node_id: 'node-a',
    projection_epoch: 1, projection_seq: 1, role: 'owner', owner_node_id: 'node-a',
    capabilities: [], sources: [],
  }))
  const endpoint = new BrowserFleetIngressEndpoint(profile(), credentials, fetcher)
  expect((await endpoint.probe()).compatible).toBe(false)
})
