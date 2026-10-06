import { readFileSync } from 'node:fs'
import { expect, test } from 'vitest'

const appCss = readFileSync('src/styles/app.css', 'utf8')

test('compact Fleet and Slot overrides do not reserve regression whitespace', () => {
  const fleetBlock = appCss.match(/\.server-card-compact \.server-card-link \{[^}]*\}/)?.[0] ?? ''
  expect(fleetBlock).not.toContain('min-height: 154px')
  expect(fleetBlock).toContain('padding-block: var(--space-3)')
  expect(appCss).toContain('.slots-surface .slot-card {\n  padding: var(--space-2) var(--space-3);\n  gap: var(--space-1);')
})

test('Server Overview badges use compact shared pills and diagnostics stay flat', () => {
  expect(appCss).not.toContain('grid-auto-columns: minmax(0, 1fr);')
  expect(appCss).toContain('.server-card-large .server-state-strip {\n  display: inline-flex;')
  expect(appCss).toContain('.server-overview-diagnostics {\n  display: block;')
  expect(appCss).toContain('background: transparent;')
})


test('status badges keep neutral borders and Slot warning cue is never double', () => {
  expect(appCss).not.toContain('border: 1px solid currentColor')
  expect(appCss).not.toContain('border-inline-start-style: double')
  expect(appCss).toContain('.slot-card.slot-cue-warning { border-inline-start: 4px solid var(--color-warning); }')
  expect(appCss).toContain('margin-inline-end: var(--space-2);')
})


test('Slot settings heading keeps status on the same row', () => {
  expect(appCss).toContain('.slot-settings-heading { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); flex-wrap: nowrap; }')
  expect(appCss).toContain('.status-badge-row { display: inline-flex; align-items: center; flex-wrap: nowrap; gap: var(--space-2); flex: 0 0 auto; }')
})
