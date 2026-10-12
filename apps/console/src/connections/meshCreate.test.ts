import { afterEach, expect, test } from 'vitest'
import { beginMeshCreate, finishMeshCreate } from './meshCreate'

afterEach(() => localStorage.clear())

test('reuse the exact durable ID after timeout/reload and clear only after confirmation', () => {
  const first = beginMeshCreate(localStorage, 'authority-a', 'node-a', 'Fleet')
  expect(first.meshId).toMatch(/^mesh-[0-9a-f]{32}$/)
  expect(beginMeshCreate(localStorage, 'authority-a', 'node-a', 'Fleet')).toEqual(first)
  finishMeshCreate(localStorage, 'authority-a', 'different-id')
  expect(beginMeshCreate(localStorage, 'authority-a', 'node-a', 'Fleet')).toEqual(first)
  finishMeshCreate(localStorage, 'authority-a', first.meshId)
  expect(beginMeshCreate(localStorage, 'authority-a', 'node-a', 'Fleet').meshId).not.toBe(first.meshId)
})

test('do not create a second mesh when an unresolved request changes parameters', () => {
  beginMeshCreate(localStorage, 'authority-a', 'node-a', 'Fleet')
  expect(() => beginMeshCreate(localStorage, 'authority-a', 'node-a', 'Renamed')).toThrow('mesh_create_reconcile_required')
  expect(() => beginMeshCreate(localStorage, 'authority-a', 'new-node', 'Fleet')).toThrow('mesh_create_reconcile_required')
  expect(beginMeshCreate(localStorage, 'authority-b', 'node-b', 'Fleet')).toBeDefined()
})

test('refuse a request whose safety journal cannot be persisted', () => {
  expect(() => beginMeshCreate({ getItem: () => null, setItem: () => { throw new Error('storage_unavailable') } }, 'a', 'a', 'Fleet')).toThrow('storage_unavailable')
})
