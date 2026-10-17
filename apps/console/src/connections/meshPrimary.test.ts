import { beforeEach, expect, test, vi } from 'vitest'
import { meshControl } from '../slots/testFixtures'
import { beginMeshHandoff, readMeshHandoff } from './meshHandoff'
import { changeMeshPrimary } from './meshPrimary'

beforeEach(() => localStorage.clear())

test('a lost primary receipt is confirmed by a fresh read before another mutation', async () => {
  let control = meshControl()
  const port = { fleetControl: vi.fn(async () => control), fleetControlMutation: vi.fn(async () => {
    control = { ...control, controlNodeId: 'beta', revisions: { ...control.revisions, topology: 2 } }
    throw new TypeError('response lost')
  }) }
  expect(await changeMeshPrimary(port, localStorage, 'mesh-prod', 'beta')).toMatchObject({ ok: true, control: { controlNodeId: 'beta' } })
  expect(port.fleetControlMutation).toHaveBeenCalledTimes(1)
  expect(localStorage.length).toBe(0)
})

test('an interrupted handoff reuses its exact durable operation and revision', async () => {
  const port = { fleetControl: vi.fn(async () => meshControl()), fleetControlMutation: vi.fn(async () => ({ ok: false, code: 'mesh_primary_handoff_pending' })) }
  expect((await changeMeshPrimary(port, localStorage, 'mesh-prod', 'beta')).ok).toBe(false)
  expect((await changeMeshPrimary(port, localStorage, 'mesh-prod', 'beta')).ok).toBe(false)
  expect(port.fleetControlMutation.mock.calls[1]).toEqual(port.fleetControlMutation.mock.calls[0])
})

test('journal failure prevents any primary transfer', async () => {
  const storage = { getItem: () => null, setItem: () => { throw new Error('storage unavailable') }, removeItem: vi.fn() }
  const port = { fleetControl: vi.fn(async () => meshControl()), fleetControlMutation: vi.fn(async () => ({ ok: true })) }
  await expect(changeMeshPrimary(port, storage, 'mesh-prod', 'beta')).rejects.toThrow('storage unavailable')
  expect(port.fleetControlMutation).not.toHaveBeenCalled()
})

test('a durably aborted transfer releases its operation for a new attempt', async () => {
  const requests: Record<string, unknown>[] = []
  const port = { fleetControl: vi.fn(async () => meshControl()),
    fleetControlMutation: vi.fn(async (path: string, body: Record<string, unknown>) => {
      expect(path).toBe('/actions/fleet/control/primary')
      requests.push(body)
      return { ok: false, code: 'mesh_operation_aborted' }
    }) }
  await changeMeshPrimary(port, localStorage, 'mesh-prod', 'beta')
  expect(localStorage.length).toBe(0)
  await changeMeshPrimary(port, localStorage, 'mesh-prod', 'beta')
  expect(requests[1].operation_id).not.toBe(requests[0].operation_id)
})


test('resuming a move after primary transfer preserves its destination', () => {
  const intent = { nodeId: 'alpha', sourceControlId: 'alpha', targetControlId: 'delta', targetFleetId: 'mesh-next', targetMeshId: 'mesh-next' }
  beginMeshHandoff(localStorage, 'alpha', intent)
  beginMeshHandoff(localStorage, 'alpha', { ...intent, sourceControlId: 'beta' })
  expect(readMeshHandoff(localStorage, 'alpha')).toEqual({ ...intent, sourceControlId: 'beta' })
  beginMeshHandoff(localStorage, 'alpha', { ...intent, sourceControlId: 'beta', targetControlId: 'epsilon' })
  expect(readMeshHandoff(localStorage, 'alpha')?.targetControlId).toBe('epsilon')
  expect(() => beginMeshHandoff(localStorage, 'alpha', { ...intent, targetMeshId: 'different' })).toThrow('control_commit_uncertain')
})
