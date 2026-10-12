import { expect, test } from 'vitest'
import { readNativeCodeReceipt } from './nativeCodeReceipt'

const receipt = { issuer_node_id: 'issuer', slot_id: 'as_one', logical_agent_id: 'la_one', revision: 12, code_generation: 2, access_code: '0042', public_name: 'Alpha', code_expires_at: null }

test('code generation is not the slot revision and a leading zero remains part of the code', () => {
  expect(readNativeCodeReceipt(receipt).value).toEqual({ code: '0042', generation: 2, issuedRevision: 12, publicName: 'Alpha', expiresAt: null })
})

test('old receipts use an explicitly unknown generation instead of inventing one from revision', () => {
  const old = { ...receipt, code_generation: undefined }
  expect(readNativeCodeReceipt(old).value.generation).toBe(0)
  expect(readNativeCodeReceipt(old).value.issuedRevision).toBe(12)
})

test('an invalid response is rejected with a safe error message that does not echo its code', () => {
  expect(() => readNativeCodeReceipt({ ...receipt, access_code: 'private-invalid-secret' })).toThrow('invalid_code_receipt')
  expect(() => readNativeCodeReceipt({ ...receipt, revision: 0 })).toThrow('invalid_code_receipt')
})
