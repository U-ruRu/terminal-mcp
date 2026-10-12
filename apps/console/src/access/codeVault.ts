export type StoredAccessCode = {
  code: string
  generation: number
  publicName?: string
  expiresAt?: string | null
  /** Native old-server fallback: a zero generation is valid only at this exact slot revision. */
  issuedRevision?: number
}

const PREFIX = 'terminal-mcp.console.access-code.v1.'

function key(logicalAgentId: string, scope = ''): string {
  if (!/^[A-Za-z0-9._:-]{1,200}$/.test(logicalAgentId)) {
    throw new Error('invalid_logical_agent_id')
  }
  if (scope.length > 1000 || /[\r\n\0]/.test(scope)) throw new Error('invalid_access_scope')
  return PREFIX + (scope ? 'scoped.' + encodeURIComponent(scope) + '.' : '') + logicalAgentId
}

function valid(value: unknown): value is StoredAccessCode {
  if (!value || typeof value !== 'object') return false
  const item = value as Record<string, unknown>
  return (
    typeof item.code === 'string'
    && /^[0-9]{4}$/.test(item.code)
    && Number.isInteger(item.generation)
    && (Number(item.generation) > 0 || Number(item.generation) === 0 && Number.isSafeInteger(item.issuedRevision) && Number(item.issuedRevision) > 0)
    && (item.expiresAt === undefined || item.expiresAt === null || typeof item.expiresAt === 'string' && Number.isFinite(Date.parse(item.expiresAt)))
    && (item.issuedRevision === undefined || Number.isSafeInteger(item.issuedRevision) && Number(item.issuedRevision) > 0)
    && (item.publicName === undefined || typeof item.publicName === 'string')
  )
}

export function saveAccessCode(
  logicalAgentId: string,
  value: StoredAccessCode,
  storage: Storage = window.localStorage,
  scope = '',
): void {
  if (!valid(value)) throw new Error('invalid_access_code')
  try { storage.setItem(key(logicalAgentId, scope), JSON.stringify(value)) }
  catch { throw new Error('access_code_storage_unavailable') }
}

export function loadAccessCode(
  logicalAgentId: string,
  observedGeneration = 0,
  storage: Storage = window.localStorage,
  scope = '',
  observedRevision = 0,
): StoredAccessCode | null {
  const storageKey = key(logicalAgentId, scope)
  try {
    const raw = storage.getItem(storageKey)
    if (!raw) return null
    const parsed: unknown = JSON.parse(raw)
    if (!valid(parsed)) {
      storage.removeItem(storageKey)
      return null
    }
    // A higher observed generation means this device holds a stale code (for example
    // after rotation from another Console). Never keep displaying that old credential.
    if (observedGeneration > parsed.generation
      || parsed.generation === 0 && observedRevision > 0 && parsed.issuedRevision !== observedRevision
      || parsed.expiresAt != null && Date.parse(parsed.expiresAt) <= Date.now()) {
      storage.removeItem(storageKey)
      return null
    }
    return parsed
  } catch {
    try { storage.removeItem(storageKey) } catch { /* Storage may be unavailable on a restricted WebView. */ }
    return null
  }
}

export function clearAccessCode(
  logicalAgentId: string,
  storage: Storage = window.localStorage,
  scope = '',
): void {
  try { storage.removeItem(key(logicalAgentId, scope)) } catch { throw new Error('access_code_storage_unavailable') }
}

export { PREFIX as ACCESS_CODE_STORAGE_PREFIX }
