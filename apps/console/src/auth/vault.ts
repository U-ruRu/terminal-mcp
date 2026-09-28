import type { StoredConnection } from './types'

export interface KeyValueStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

const STORAGE_KEY = 'terminal-mcp.console.connection.v1'
const CREDENTIAL_STORAGE_PREFIX = 'terminal-mcp.console.credential.v1.'

function isStoredConnection(value: unknown): value is StoredConnection {
  if (!value || typeof value !== 'object') return false
  const item = value as Record<string, unknown>
  return (
    typeof item.origin === 'string' &&
    typeof item.deviceId === 'string' &&
    typeof item.clientId === 'string' &&
    typeof item.deviceLabel === 'string' &&
    typeof item.scope === 'string' &&
    typeof item.refreshToken === 'string' &&
    typeof item.pairedAt === 'number'
  )
}

function credentialStorageKey(reference: string): string {
  if (!/^[A-Za-z0-9._-]{1,160}$/.test(reference)) {
    throw new Error('invalid_credential_reference')
  }
  return CREDENTIAL_STORAGE_PREFIX + reference
}

export class BrowserCredentialVault {
  constructor(
    private readonly storage: KeyValueStorage = window.localStorage,
    private readonly storageKey: string = STORAGE_KEY,
  ) {}

  static forReference(
    reference: string,
    storage: KeyValueStorage = window.localStorage,
  ): BrowserCredentialVault {
    return new BrowserCredentialVault(storage, credentialStorageKey(reference))
  }

  load(): StoredConnection | null {
    const raw = this.storage.getItem(this.storageKey)
    if (!raw) return null
    try {
      const value: unknown = JSON.parse(raw)
      if (!isStoredConnection(value)) {
        this.clear()
        return null
      }
      return value
    } catch {
      this.clear()
      return null
    }
  }

  save(connection: StoredConnection): void {
    this.storage.setItem(this.storageKey, JSON.stringify(connection))
  }

  clear(): void {
    this.storage.removeItem(this.storageKey)
  }
}

export { CREDENTIAL_STORAGE_PREFIX, STORAGE_KEY, credentialStorageKey }
