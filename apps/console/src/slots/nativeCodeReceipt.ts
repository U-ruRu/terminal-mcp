import type { StoredAccessCode } from '../access/codeVault'
import type { JsonRecord } from '../api/models'

export function nativeCodeScope(issuerNodeId: string, slotId: string): string {
  return `access-mesh:${issuerNodeId}:${slotId}`
}

function identifier(value: unknown): string {
  if (typeof value !== 'string' || !/^[A-Za-z0-9._:-]{1,200}$/.test(value)) throw new Error('invalid_code_receipt')
  return value
}

export function readNativeCodeReceipt(payload: JsonRecord): { slotId: string; issuerNodeId: string; revision: number; value: StoredAccessCode } {
  const slotId = identifier(payload.slot_id)
  const issuerNodeId = identifier(payload.issuer_node_id)
  if (typeof payload.access_code !== 'string' || !/^\d{4}$/.test(payload.access_code)) throw new Error('invalid_code_receipt')
  if (!Number.isSafeInteger(payload.revision) || Number(payload.revision) < 1) throw new Error('invalid_code_receipt')
  const generation = payload.code_generation === undefined ? 0 : payload.code_generation
  if (!Number.isSafeInteger(generation) || Number(generation) < 0 || generation === 0 && payload.code_generation !== undefined) throw new Error('invalid_code_receipt')
  const expiresAt = payload.code_expires_at
  if (expiresAt !== undefined && expiresAt !== null && (typeof expiresAt !== 'string' || !Number.isFinite(Date.parse(expiresAt)))) throw new Error('invalid_code_receipt')
  return {
    slotId, issuerNodeId, revision: Number(payload.revision),
    value: {
      code: payload.access_code,
      generation: Number(generation),
      publicName: typeof payload.public_name === 'string' ? payload.public_name : undefined,
      expiresAt: expiresAt as string | null | undefined,
      issuedRevision: Number(payload.revision),
    },
  }
}
