import { expect, test } from 'vitest'

import type { KeyValueStorage } from '../auth/vault'
import { BrowserDiagnosticJournal, DIAGNOSTIC_EVENT_LIMIT, diagnosticErrorCode } from './journal'

class MemoryStorage implements KeyValueStorage {
  values = new Map<string, string>()
  getItem(key: string) { return this.values.get(key) ?? null }
  setItem(key: string, value: string) { this.values.set(key, value) }
  removeItem(key: string) { this.values.delete(key) }
}

test('persists a bounded last-100 diagnostic ring and filters global plus server events', () => {
  const storage = new MemoryStorage()
  let second = 0
  const journal = new BrowserDiagnosticJournal(storage, () => new Date(1_700_000_000_000 + second++ * 1000))
  for (let index = 0; index < DIAGNOSTIC_EVENT_LIMIT + 5; index += 1) {
    journal.append({ type: 'tick', instanceId: index % 2 ? 'a' : 'b', detail: String(index) })
  }
  journal.append({ type: 'app_foreground' })

  const reloaded = new BrowserDiagnosticJournal(storage)
  expect(reloaded.list()).toHaveLength(100)
  expect(reloaded.list().at(-1)?.type).toBe('app_foreground')
  expect(reloaded.list('a').every((event) => !event.instanceId || event.instanceId === 'a')).toBe(true)
})

test('records app update boundary and exports readable text without structured secrets', () => {
  const storage = new MemoryStorage()
  const journal = new BrowserDiagnosticJournal(storage, () => new Date('2026-09-30T06:00:00Z'))
  journal.recordApplicationStart('0.2.9', '9')
  journal.recordApplicationStart('0.2.10', '10')
  const text = journal.exportText()
  expect(text).toContain('app_updated')
  expect(text).toContain('0.2.9 (9) -> 0.2.10 (10)')
  expect(text).not.toContain('refreshToken')
})

test('extracts stable diagnostic error codes', () => {
  expect(diagnosticErrorCode('Cannot resolve host. Diagnostic: dns_error.')).toBe('dns_error')
  expect(diagnosticErrorCode('network_error')).toBe('network_error')
  expect(diagnosticErrorCode('long human readable message')).toBeUndefined()
})


test('clear removes only diagnostic events and preserves the app build marker', () => {
  const storage = new MemoryStorage()
  const journal = new BrowserDiagnosticJournal(storage, () => new Date('2026-09-30T07:00:00Z'))
  journal.recordApplicationStart('0.2.8', '10')
  journal.append({ type: 'network_error', code: 'network_error' })
  expect(journal.list().length).toBeGreaterThan(0)

  journal.clear()
  expect(journal.list()).toEqual([])

  journal.recordApplicationStart('0.2.9', '11')
  expect(journal.list().map((event) => event.type)).toEqual(['app_updated', 'app_started'])
  expect(journal.exportText()).toContain('0.2.8 (10) -> 0.2.9 (11)')
})
