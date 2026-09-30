import type { KeyValueStorage } from '../auth/vault'

export const FLEET_V1_CLIENT_FLAG_KEY = 'terminal-mcp.console.fleet-v1-client-enabled.v1'

export function fleetV1ClientEnabled(
  storage: KeyValueStorage = window.localStorage,
): boolean {
  return storage.getItem(FLEET_V1_CLIENT_FLAG_KEY) === 'true'
}

export function setFleetV1ClientEnabled(
  enabled: boolean,
  storage: KeyValueStorage = window.localStorage,
): void {
  if (enabled) storage.setItem(FLEET_V1_CLIENT_FLAG_KEY, 'true')
  else storage.removeItem(FLEET_V1_CLIENT_FLAG_KEY)
}
