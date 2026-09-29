export const PRODUCTION_CONSOLE_ORIGIN = 'https://terminal-console.solvenger.app'

export type CanonicalPairingPayload = {
  v: 1
  server: string
  name: string
  secret: string
}

export type ParsedPairingLink = {
  origin: string
  name: string
  secret: string
}

function decodeBase64Url(value: string): string {
  if (!/^[A-Za-z0-9_-]+$/.test(value) || value.length > 16_384) throw new Error('invalid_pairing_link')
  const padding = '='.repeat((4 - (value.length % 4)) % 4)
  try {
    const binary = atob(value.replaceAll('-', '+').replaceAll('_', '/') + padding)
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0))
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    throw new Error('invalid_pairing_link')
  }
}

function canonicalHttpsOrigin(value: unknown): string {
  if (typeof value !== 'string') throw new Error('invalid_pairing_link')
  let url: URL
  try { url = new URL(value) } catch { throw new Error('invalid_pairing_link') }
  if (
    url.protocol !== 'https:' ||
    url.username ||
    url.password ||
    url.pathname !== '/' ||
    url.search ||
    url.hash
  ) throw new Error('invalid_pairing_link')
  return url.origin
}

export function parseCanonicalPairingLink(
  value: string,
  trustedOuterOrigin = PRODUCTION_CONSOLE_ORIGIN,
): ParsedPairingLink {
  let outer: URL
  try { outer = new URL(value) } catch { throw new Error('invalid_pairing_link') }
  if (
    outer.protocol !== 'https:' ||
    outer.origin !== trustedOuterOrigin ||
    outer.username ||
    outer.password ||
    outer.pathname !== '/connect' ||
    outer.search ||
    !outer.hash ||
    outer.hash === '#'
  ) throw new Error('invalid_pairing_link')

  let payload: unknown
  try { payload = JSON.parse(decodeBase64Url(outer.hash.slice(1))) } catch { throw new Error('invalid_pairing_link') }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('invalid_pairing_link')
  const item = payload as Record<string, unknown>
  if (item.v !== 1) throw new Error('invalid_pairing_link')

  const origin = canonicalHttpsOrigin(item.server)
  if (typeof item.name !== 'string') throw new Error('invalid_pairing_link')
  const name = item.name.trim()
  if (!name || name.length > 120) throw new Error('invalid_pairing_link')
  if (typeof item.secret !== 'string' || !/^[A-Za-z0-9_-]{20,256}$/.test(item.secret)) {
    throw new Error('invalid_pairing_link')
  }

  return { origin, name, secret: item.secret }
}

export function inspectCanonicalPairingLink(
  value: string,
  trustedOuterOrigin = PRODUCTION_CONSOLE_ORIGIN,
): { origin: string; name: string } {
  const { origin, name } = parseCanonicalPairingLink(value, trustedOuterOrigin)
  return { origin, name }
}
