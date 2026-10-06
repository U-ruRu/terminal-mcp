export const PRODUCTION_CONSOLE_ORIGIN = 'https://terminal-console.solvenger.app'

const MAX_ENCODED_PAYLOAD_CHARS = 8_192
const MAX_DECODED_PAYLOAD_BYTES = 6_144
const MAX_PROFILE_BYTES = 4_096
const MAX_CONTAINER_DEPTH = 4
const MAX_OBJECT_MEMBERS = 64
const MAX_KEY_CHARS = 64
const MAX_STRING_CHARS = 1_024
const MAX_ARRAY_ENTRIES = 32

export type SafeJsonValue =
  | null
  | boolean
  | number
  | string
  | SafeJsonValue[]
  | { [key: string]: SafeJsonValue }

export type SafePairingProfile = { [key: string]: SafeJsonValue }

export type CanonicalPairingPayload = {
  v: 1
  server: string
  name: string
  secret: string
  profile?: SafePairingProfile | null
  ephemeral?: unknown
}

export type ParsedPairingLink = {
  origin: string
  name: string
  secret: string
  profile: SafePairingProfile | null
}

function invalidPairingLink(): never {
  throw new Error('invalid_pairing_link')
}

function decodeBase64Url(value: string): string {
  if (
    !/^[A-Za-z0-9_-]+$/.test(value)
    || value.length > MAX_ENCODED_PAYLOAD_CHARS
  ) invalidPairingLink()
  const padding = '='.repeat((4 - (value.length % 4)) % 4)
  try {
    const binary = atob(value.replaceAll('-', '+').replaceAll('_', '/') + padding)
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0))
    if (bytes.length > MAX_DECODED_PAYLOAD_BYTES) invalidPairingLink()
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    invalidPairingLink()
  }
}

function canonicalHttpsOrigin(value: unknown): string {
  if (typeof value !== 'string') invalidPairingLink()
  let url: URL
  try { url = new URL(value) } catch { invalidPairingLink() }
  if (
    url.protocol !== 'https:'
    || url.username
    || url.password
    || url.pathname !== '/'
    || url.search
    || url.hash
  ) invalidPairingLink()
  return url.origin
}

const PROTOTYPE_CONTROL_KEYS = new Set(['__proto__', 'prototype', 'constructor'])
const SENSITIVE_PERSISTED_KEY_MARKERS = [
  'secret',
  'password',
  'token',
  'verifier',
  'credential',
  'bearer',
  'apikey',
  'accesskey',
  'privatekey',
  'signingkey',
  'authorizationcode',
  'authcode',
  'assertion',
  'nonce',
  'challenge',
  'cookie',
  'sessionid',
] as const

function forbiddenPersistedKey(key: string): boolean {
  const lowered = key.toLowerCase()
  if (PROTOTYPE_CONTROL_KEYS.has(lowered)) return true
  const compact = lowered.replace(/[^a-z0-9]/g, '')
  return SENSITIVE_PERSISTED_KEY_MARKERS.some((marker) => compact.includes(marker))
}

function normalizeSafeJson(value: unknown, depth: number): SafeJsonValue {
  if (value === null || typeof value === 'boolean') return value
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) invalidPairingLink()
    return value
  }
  if (typeof value === 'string') {
    if (value.length > MAX_STRING_CHARS) invalidPairingLink()
    return value
  }
  if (depth > MAX_CONTAINER_DEPTH) invalidPairingLink()
  if (Array.isArray(value)) {
    if (value.length > MAX_ARRAY_ENTRIES) invalidPairingLink()
    return value.map((item) => normalizeSafeJson(item, depth + 1))
  }
  if (!value || typeof value !== 'object') invalidPairingLink()

  const entries = Object.entries(value as Record<string, unknown>)
  if (entries.length > MAX_OBJECT_MEMBERS) invalidPairingLink()
  const normalized = Object.create(null) as Record<string, SafeJsonValue>
  for (const [key, item] of entries) {
    if (
      !key
      || key.length > MAX_KEY_CHARS
      || forbiddenPersistedKey(key)
    ) invalidPairingLink()
    normalized[key] = normalizeSafeJson(item, depth + 1)
  }
  return normalized
}

export function normalizeSafePairingProfile(value: unknown): SafePairingProfile | null {
  if (value === undefined) return { extensions: {} }
  if (value === null) return null
  if (Array.isArray(value) || typeof value !== 'object') invalidPairingLink()

  const normalized = normalizeSafeJson(value, 1)
  if (!normalized || Array.isArray(normalized) || typeof normalized !== 'object') invalidPairingLink()
  const profile = normalized as SafePairingProfile

  if (!Object.prototype.hasOwnProperty.call(profile, 'extensions')) {
    profile.extensions = {}
  } else if (
    profile.extensions !== null
    && (
      Array.isArray(profile.extensions)
      || typeof profile.extensions !== 'object'
    )
  ) invalidPairingLink()

  if (new TextEncoder().encode(JSON.stringify(profile)).length > MAX_PROFILE_BYTES) {
    invalidPairingLink()
  }
  return profile
}

export function cloneSafePairingProfile(
  profile: SafePairingProfile | null | undefined,
): SafePairingProfile | null {
  if (profile === undefined) return { extensions: {} }
  if (profile === null) return null
  return JSON.parse(JSON.stringify(profile)) as SafePairingProfile
}

export function parseCanonicalPairingLink(
  value: string,
  trustedOuterOrigin = PRODUCTION_CONSOLE_ORIGIN,
): ParsedPairingLink {
  let outer: URL
  try { outer = new URL(value) } catch { invalidPairingLink() }
  if (
    outer.protocol !== 'https:'
    || outer.origin !== trustedOuterOrigin
    || outer.username
    || outer.password
    || outer.pathname !== '/connect'
    || outer.search
    || !outer.hash
    || outer.hash === '#'
  ) invalidPairingLink()

  let payload: unknown
  try { payload = JSON.parse(decodeBase64Url(outer.hash.slice(1))) } catch (error) {
    if (error instanceof Error && error.message === 'invalid_pairing_link') throw error
    invalidPairingLink()
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) invalidPairingLink()
  const item = payload as Record<string, unknown>
  if (item.v !== 1) {
    if (typeof item.v === 'number' && Number.isInteger(item.v)) {
      throw new Error('unsupported_pairing_version')
    }
    invalidPairingLink()
  }

  const origin = canonicalHttpsOrigin(item.server)
  if (typeof item.name !== 'string') invalidPairingLink()
  const name = item.name.trim()
  if (!name || name.length > 120) invalidPairingLink()
  if (typeof item.secret !== 'string' || !/^[A-Za-z0-9_-]{20,256}$/.test(item.secret)) {
    invalidPairingLink()
  }

  return {
    origin,
    name,
    secret: item.secret,
    profile: normalizeSafePairingProfile(item.profile),
  }
}

export function inspectCanonicalPairingLink(
  value: string,
  trustedOuterOrigin = PRODUCTION_CONSOLE_ORIGIN,
): { origin: string; name: string } {
  const { origin, name } = parseCanonicalPairingLink(value, trustedOuterOrigin)
  return { origin, name }
}
