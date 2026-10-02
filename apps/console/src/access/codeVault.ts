export type StoredAccessCode = {
  code: string
  generation: number
  publicName?: string
}

const PREFIX = 'terminal-mcp.console.access-code.v1.'

function key(logicalAgentId: string): string {
  if (!/^[A-Za-z0-9._:-]{1,200}$/.test(logicalAgentId)) {
    throw new Error('invalid_logical_agent_id')
  }
  return PREFIX + logicalAgentId
}

function valid(value: unknown): value is StoredAccessCode {
  if (!value || typeof value !== 'object') return false
  const item = value as Record<string, unknown>
  return (
    typeof item.code === 'string'
    && /^[0-9]{4}$/.test(item.code)
    && Number.isInteger(item.generation)
    && Number(item.generation) > 0
    && (item.publicName === undefined || typeof item.publicName === 'string')
  )
}

export function saveAccessCode(
  logicalAgentId: string,
  value: StoredAccessCode,
  storage: Storage = window.localStorage,
): void {
  if (!valid(value)) throw new Error('invalid_access_code')
  storage.setItem(key(logicalAgentId), JSON.stringify(value))
}

export function loadAccessCode(
  logicalAgentId: string,
  observedGeneration = 0,
  storage: Storage = window.localStorage,
): StoredAccessCode | null {
  const storageKey = key(logicalAgentId)
  const raw = storage.getItem(storageKey)
  if (!raw) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!valid(parsed)) {
      storage.removeItem(storageKey)
      return null
    }
    // A higher observed generation means this device holds a stale code (for example
    // after rotation from another Console). Never keep displaying that old credential.
    if (observedGeneration > parsed.generation) {
      storage.removeItem(storageKey)
      return null
    }
    return parsed
  } catch {
    storage.removeItem(storageKey)
    return null
  }
}

export function clearAccessCode(
  logicalAgentId: string,
  storage: Storage = window.localStorage,
): void {
  storage.removeItem(key(logicalAgentId))
}

export { PREFIX as ACCESS_CODE_STORAGE_PREFIX }
