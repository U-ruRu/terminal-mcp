import { expect, test } from 'vitest'
import type { KeyValueStorage } from '../auth/vault'
import {
  FLEET_V1_CLIENT_FLAG_KEY,
  fleetV1ClientEnabled,
  setFleetV1ClientEnabled,
} from './flags'

class MemoryStorage implements KeyValueStorage {
  readonly values = new Map<string, string>()
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
}

test('fleet v1 client flag defaults off and rollback leaves cache/profile data untouched', () => {
  const storage = new MemoryStorage()
  storage.setItem('terminal-mcp.console.connections.v1', '{"version":1}')
  storage.setItem('terminal-mcp.console.fleet.v1.cache-marker', 'keep')
  expect(fleetV1ClientEnabled(storage)).toBe(false)
  setFleetV1ClientEnabled(true, storage)
  expect(fleetV1ClientEnabled(storage)).toBe(true)
  setFleetV1ClientEnabled(false, storage)
  expect(fleetV1ClientEnabled(storage)).toBe(false)
  expect(storage.getItem(FLEET_V1_CLIENT_FLAG_KEY)).toBeNull()
  expect(storage.getItem('terminal-mcp.console.connections.v1')).toBe('{"version":1}')
  expect(storage.getItem('terminal-mcp.console.fleet.v1.cache-marker')).toBe('keep')
})
