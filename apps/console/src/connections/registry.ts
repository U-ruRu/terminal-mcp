import { generateDevicePublicKey } from '../auth/pairing'
import { AuthTransportError, PairingTransport } from '../auth/transport'
import type { StoredConnection } from '../auth/types'
import {
  BrowserCredentialVault,
  STORAGE_KEY as LEGACY_CONNECTION_KEY,
  type KeyValueStorage,
} from '../auth/vault'
import { parseCanonicalPairingLink } from './pairingLink'
import type {
  ConnectionProfile,
  ConnectionProfileMetadata,
  ConnectionRegistryDocument,
  PairedProfile,
  ProfileRestoreResult,
} from './types'

const REGISTRY_STORAGE_KEY = 'terminal-mcp.console.connections.v1'

export class ConnectionRegistryError extends Error {
  constructor(readonly code: string) {
    super(code)
    this.name = 'ConnectionRegistryError'
  }
}

function canonicalOrigin(value: string): string {
  let url: URL
  try {
    url = new URL(value)
  } catch {
    throw new ConnectionRegistryError('invalid_origin')
  }
  if (
    !['http:', 'https:'].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.pathname !== '/' ||
    url.search ||
    url.hash
  ) {
    throw new ConnectionRegistryError('invalid_origin')
  }
  return url.origin
}

function defaultDisplayName(origin: string): string {
  return new URL(origin).host
}

function safeMetadata(connection: StoredConnection): ConnectionProfileMetadata {
  return {
    deviceId: connection.deviceId,
    clientId: connection.clientId,
    deviceLabel: connection.deviceLabel,
    scope: connection.scope,
    pairedAt: connection.pairedAt,
  }
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function isProfile(value: unknown): value is ConnectionProfile {
  if (!value || typeof value !== 'object') return false
  const item = value as Record<string, unknown>
  const metadata = item.metadata
  if (!metadata || typeof metadata !== 'object') return false
  const meta = metadata as Record<string, unknown>
  try {
    if (canonicalOrigin(String(item.origin ?? '')) !== item.origin) return false
  } catch {
    return false
  }
  return (
    typeof item.instanceId === 'string' &&
    item.instanceId.length > 0 &&
    typeof item.displayName === 'string' &&
    item.displayName.trim().length > 0 &&
    typeof item.credentialRef === 'string' &&
    /^[A-Za-z0-9._-]{1,160}$/.test(item.credentialRef) &&
    typeof meta.deviceId === 'string' &&
    typeof meta.clientId === 'string' &&
    typeof meta.deviceLabel === 'string' &&
    typeof meta.scope === 'string' &&
    isFiniteNumber(meta.pairedAt) &&
    isFiniteNumber(item.createdAt) &&
    isFiniteNumber(item.updatedAt)
  )
}

function parseDocument(raw: string): ConnectionRegistryDocument {
  let value: unknown
  try {
    value = JSON.parse(raw)
  } catch {
    throw new ConnectionRegistryError('invalid_registry')
  }
  if (!value || typeof value !== 'object') {
    throw new ConnectionRegistryError('invalid_registry')
  }
  const document = value as Record<string, unknown>
  if (document.version !== 1 || !Array.isArray(document.profiles)) {
    throw new ConnectionRegistryError('unsupported_registry_version')
  }
  if (!document.profiles.every(isProfile)) {
    throw new ConnectionRegistryError('invalid_registry')
  }

  const origins = new Set<string>()
  const instanceIds = new Set<string>()
  const credentialRefs = new Set<string>()
  for (const profile of document.profiles) {
    if (
      origins.has(profile.origin) ||
      instanceIds.has(profile.instanceId) ||
      credentialRefs.has(profile.credentialRef)
    ) {
      throw new ConnectionRegistryError('conflicting_registry')
    }
    origins.add(profile.origin)
    instanceIds.add(profile.instanceId)
    credentialRefs.add(profile.credentialRef)
  }

  return {
    version: 1,
    profiles: document.profiles.map((profile) => ({
      ...profile,
      metadata: { ...profile.metadata },
    })),
  }
}

function parsePairingLink(value: string): { origin: string; name: string; secret: string } {
  try {
    return parseCanonicalPairingLink(value)
  } catch {
    throw new ConnectionRegistryError('invalid_pairing_link')
  }
}

export class BrowserConnectionRegistry {
  constructor(
    private readonly storage: KeyValueStorage = window.localStorage,
    private readonly now: () => number = Date.now,
    private readonly makeId: () => string = () => crypto.randomUUID(),
  ) {}

  list(): ConnectionProfile[] {
    return this.document().profiles.map((profile) => ({
      ...profile,
      metadata: { ...profile.metadata },
    }))
  }

  get(instanceId: string): ConnectionProfile | null {
    return this.list().find((profile) => profile.instanceId === instanceId) ?? null
  }

  credential(instanceId: string): StoredConnection | null {
    const profile = this.get(instanceId)
    if (!profile) return null
    const connection = BrowserCredentialVault.forReference(
      profile.credentialRef,
      this.storage,
    ).load()
    if (!connection) return null
    if (
      canonicalOrigin(connection.origin) !== profile.origin ||
      connection.deviceId !== profile.metadata.deviceId ||
      connection.clientId !== profile.metadata.clientId
    ) {
      throw new ConnectionRegistryError('credential_profile_mismatch')
    }
    return connection
  }

  add(connection: StoredConnection, displayName?: string): ConnectionProfile {
    const document = this.document()
    const origin = canonicalOrigin(connection.origin)
    this.assertOriginAvailable(document, origin)
    const instanceId = this.uniqueId(document)
    const credentialRef = 'profile-' + instanceId
    const timestamp = this.now()
    const normalizedConnection: StoredConnection = { ...connection, origin }
    const profile: ConnectionProfile = {
      instanceId,
      origin,
      displayName: this.cleanDisplayName(displayName ?? defaultDisplayName(origin)),
      credentialRef,
      metadata: safeMetadata(normalizedConnection),
      createdAt: timestamp,
      updatedAt: timestamp,
    }

    const credentialVault = BrowserCredentialVault.forReference(credentialRef, this.storage)
    credentialVault.save(normalizedConnection)
    try {
      this.persist({ version: 1, profiles: [...document.profiles, profile] })
    } catch (error) {
      credentialVault.clear()
      throw error
    }
    return { ...profile, metadata: { ...profile.metadata } }
  }

  async restore(
    instanceId: string,
    transport = new PairingTransport(),
  ): Promise<ProfileRestoreResult> {
    const profile = this.get(instanceId)
    if (!profile) return { status: 'missing' }

    const credentialVault = BrowserCredentialVault.forReference(
      profile.credentialRef,
      this.storage,
    )
    const connection = credentialVault.load()
    if (!connection) return { status: 'revoked', profile }

    if (
      canonicalOrigin(connection.origin) !== profile.origin ||
      connection.deviceId !== profile.metadata.deviceId ||
      connection.clientId !== profile.metadata.clientId
    ) {
      throw new ConnectionRegistryError('credential_profile_mismatch')
    }

    try {
      const refreshed = await transport.refresh(
        connection.origin,
        connection.clientId,
        connection.refreshToken,
      )
      const rotated: StoredConnection = {
        ...connection,
        scope: refreshed.scope,
        refreshToken: refreshed.refresh_token,
      }
      credentialVault.save(rotated)
      return {
        status: 'connected',
        profile,
        accessToken: refreshed.access_token,
        accessExpiresAt: this.now() + Math.max(0, refreshed.expires_in) * 1000,
      }
    } catch (error) {
      const transportError =
        error instanceof AuthTransportError
          ? error
          : new AuthTransportError(0, 'restore_failed')
      if (transportError.revoked || transportError.expired) {
        credentialVault.clear()
        return {
          status: transportError.revoked ? 'revoked' : 'expired',
          profile,
        }
      }
      return {
        status: 'error',
        profile,
        retryable: transportError.retryable,
        message: transportError.message,
      }
    }
  }

  async pairAndAdd(
    pairingLink: string,
    deviceLabel: string,
    displayName?: string,
    transport = new PairingTransport(),
    keyFactory: () => Promise<string> = generateDevicePublicKey,
  ): Promise<PairedProfile> {
    const { origin, name, secret } = parsePairingLink(pairingLink)
    const document = this.document()
    const existing = document.profiles.find((profile) => profile.origin === origin)
    const resolvedName = this.cleanDisplayName(displayName ?? name ?? existing?.displayName ?? defaultDisplayName(origin))

    const publicKey = await keyFactory()
    const exchanged = await transport.exchange(origin, secret, publicKey, deviceLabel)
    const pairedAt = this.now()
    const connection: StoredConnection = {
      origin,
      deviceId: exchanged.device_id,
      clientId: exchanged.client_id,
      deviceLabel,
      scope: exchanged.scope,
      refreshToken: exchanged.refresh_token,
      pairedAt,
    }

    let profile: ConnectionProfile
    if (!existing) {
      profile = this.add(connection, resolvedName)
    } else {
      const credentialVault = BrowserCredentialVault.forReference(existing.credentialRef, this.storage)
      const previous = credentialVault.load()
      profile = {
        ...existing,
        displayName: resolvedName,
        metadata: safeMetadata(connection),
        updatedAt: pairedAt,
      }
      credentialVault.save(connection)
      try {
        this.persist({
          version: 1,
          profiles: document.profiles.map((item) => item.instanceId === existing.instanceId ? profile : item),
        })
      } catch (error) {
        if (previous) credentialVault.save(previous)
        else credentialVault.clear()
        throw error
      }
    }

    return {
      profile: { ...profile, metadata: { ...profile.metadata } },
      accessToken: exchanged.access_token,
      accessExpiresAt: pairedAt + Math.max(0, exchanged.expires_in) * 1000,
    }
  }

  rename(instanceId: string, displayName: string): ConnectionProfile {
    const document = this.document()
    const index = document.profiles.findIndex((profile) => profile.instanceId === instanceId)
    if (index < 0) throw new ConnectionRegistryError('profile_not_found')
    const updated: ConnectionProfile = {
      ...document.profiles[index],
      displayName: this.cleanDisplayName(displayName),
      updatedAt: this.now(),
    }
    const profiles = document.profiles.slice()
    profiles[index] = updated
    this.persist({ version: 1, profiles })
    return { ...updated, metadata: { ...updated.metadata } }
  }

  remove(instanceId: string): boolean {
    const document = this.document()
    const profile = document.profiles.find((item) => item.instanceId === instanceId)
    if (!profile) return false
    const profiles = document.profiles.filter((item) => item.instanceId !== instanceId)
    this.persist({ version: 1, profiles })
    BrowserCredentialVault.forReference(profile.credentialRef, this.storage).clear()
    return true
  }

  disconnect(instanceId: string): boolean {
    return this.remove(instanceId)
  }

  private document(): ConnectionRegistryDocument {
    const raw = this.storage.getItem(REGISTRY_STORAGE_KEY)
    if (raw) return parseDocument(raw)
    return this.migrateLegacyConnection()
  }

  private migrateLegacyConnection(): ConnectionRegistryDocument {
    const legacyVault = new BrowserCredentialVault(this.storage)
    const connection = legacyVault.load()
    if (!connection) return { version: 1, profiles: [] }

    const origin = canonicalOrigin(connection.origin)
    const instanceId = this.uniqueId({ version: 1, profiles: [] })
    const credentialRef = 'profile-' + instanceId
    const timestamp = this.now()
    const normalizedConnection: StoredConnection = { ...connection, origin }
    const profile: ConnectionProfile = {
      instanceId,
      origin,
      displayName: defaultDisplayName(origin),
      credentialRef,
      metadata: safeMetadata(normalizedConnection),
      createdAt: timestamp,
      updatedAt: timestamp,
    }
    const credentialVault = BrowserCredentialVault.forReference(credentialRef, this.storage)
    credentialVault.save(normalizedConnection)
    try {
      const migrated: ConnectionRegistryDocument = { version: 1, profiles: [profile] }
      this.persist(migrated)
      legacyVault.clear()
      return migrated
    } catch (error) {
      credentialVault.clear()
      if (!this.storage.getItem(LEGACY_CONNECTION_KEY)) legacyVault.save(connection)
      throw error
    }
  }

  private persist(document: ConnectionRegistryDocument): void {
    this.storage.setItem(REGISTRY_STORAGE_KEY, JSON.stringify(document))
  }

  private assertOriginAvailable(document: ConnectionRegistryDocument, origin: string): void {
    if (document.profiles.some((profile) => profile.origin === origin)) {
      throw new ConnectionRegistryError('duplicate_origin')
    }
  }

  private cleanDisplayName(value: string): string {
    const cleaned = value.trim()
    if (!cleaned) throw new ConnectionRegistryError('invalid_display_name')
    return cleaned.slice(0, 120)
  }

  private uniqueId(document: ConnectionRegistryDocument): string {
    for (let attempt = 0; attempt < 8; attempt += 1) {
      const candidate = this.makeId().trim()
      if (
        /^[A-Za-z0-9._-]{1,120}$/.test(candidate) &&
        !document.profiles.some((profile) => profile.instanceId === candidate)
      ) {
        return candidate
      }
    }
    throw new ConnectionRegistryError('instance_id_collision')
  }
}

export { REGISTRY_STORAGE_KEY, canonicalOrigin, parsePairingLink }
