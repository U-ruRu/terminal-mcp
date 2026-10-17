import { afterEach,expect,test } from 'vitest'
import { beginMeshCreate } from './meshCreate'

afterEach(() => localStorage.clear())

test('reopening a pending mesh creation preserves its durable request ID', () => {
  const first = beginMeshCreate(localStorage, 'authority-a', 'node-a', 'Fleet')

  const restored = beginMeshCreate(localStorage, 'authority-a', 'node-a', 'Fleet')

  expect(restored).toEqual(first)
  expect(restored.meshId).toMatch(/^mesh-[0-9a-f]{32}$/)
})
test('refuse a request whose safety journal cannot be persisted', () => {
  expect(() => beginMeshCreate({ getItem: () => null, setItem: () => { throw new Error('storage_unavailable') } }, 'a', 'a', 'Fleet')).toThrow('storage_unavailable')
})
