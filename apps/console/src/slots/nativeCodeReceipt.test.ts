import { expect,test } from 'vitest'
import { readNativeCodeReceipt } from './nativeCodeReceipt'

const receipt = { issuer_node_id: 'issuer', slot_id: 'as_one', revision: 12, code_generation: 2, access_code: '0042', public_name: 'Alpha', code_expires_at: null }

test('code generation is not the slot revision and a leading zero remains part of the code', () => {
  expect(readNativeCodeReceipt(receipt).value).toEqual({ code: '0042', generation: 2, issuedRevision: 12, publicName: 'Alpha', expiresAt: null })
})
