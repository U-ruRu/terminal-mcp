import { expect, test } from 'vitest'
import { beginMeshHandoff, readMeshHandoff } from './meshHandoff'

test('unresolved target survives reopening and cannot be replaced before reconciliation', () => {
  const values = new Map<string, string>()
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value) } }
  const intent = { nodeId: 'member', sourceControlId: 'old', targetControlId: 'new', targetFleetId: 'public-namespace', targetMeshId: 'mesh-new' }
  beginMeshHandoff(storage, 'member', intent)
  expect(readMeshHandoff(storage, 'member')).toEqual(intent)
  expect(() => beginMeshHandoff(storage, 'member', { ...intent, targetMeshId: 'different' })).toThrow('control_commit_uncertain')
})
