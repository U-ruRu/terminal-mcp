import { expect,test } from 'vitest'

import type { KeyValueStorage } from '../auth/vault'
import { BrowserDiagnosticJournal,DIAGNOSTIC_EVENT_LIMIT } from './journal'

class MemoryStorage implements KeyValueStorage {
  values = new Map<string, string>()
  getItem(key: string) { return this.values.get(key) ?? null }
  setItem(key: string, value: string) { this.values.set(key, value) }
  removeItem(key: string) { this.values.delete(key) }
}

test('appending at the journal limit replaces the oldest diagnostic event', () => {
  const storage = new MemoryStorage()
  const events = Array.from({ length: DIAGNOSTIC_EVENT_LIMIT }, (_, index) => ({ seq: index + 1, at: '2026-10-01T00:00:00Z', type: 'tick' }))
  storage.setItem('terminal-mcp.console.diagnostics.v1', JSON.stringify({ version: 1, nextSeq: DIAGNOSTIC_EVENT_LIMIT + 1, events }))
  const journal = new BrowserDiagnosticJournal(storage)

  journal.append({ type: 'app_foreground' })

  expect(journal.list()).toHaveLength(DIAGNOSTIC_EVENT_LIMIT)
  expect(journal.list()[0].seq).toBe(2)
  expect(journal.list().at(-1)?.type).toBe('app_foreground')
})
test('clearing diagnostics preserves the current app build marker', () => {
  const storage = new MemoryStorage()
  const journal = new BrowserDiagnosticJournal(storage)
  journal.recordApplicationStart('0.14.4', '100')

  journal.clear()

  expect(journal.list()).toEqual([])
  expect(storage.getItem('terminal-mcp.console.last-build.v1')).toBe('0.14.4 (100)')
})
