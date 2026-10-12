import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { I18nProvider } from '../i18n/I18nProvider'
import { en } from '../i18n/catalogs'
import { nativeInheritance, nativeSlot } from '../slots/testFixtures'
import { PolicyInheritance } from '../slots/PolicyInheritance'
import { AccessMeshSlotControls } from './AccessMeshPolicyControls'

afterEach(cleanup)
test('slot inheritance labels the server identity and its own revision separately from effective Mesh source', () => {
  const inherited = nativeInheritance('inherit', 'mesh')
  inherited.parent = { ...inherited.parent!, source: 'server', id: 'issuer-alpha', revision: 7, version: 'server:issuer-alpha:7' }
  render(<I18nProvider><PolicyInheritance value={inherited} /></I18nProvider>)
  expect(screen.getByText(/issuer-alpha/)).toHaveTextContent('Server defaults · issuer-alpha')
  expect(screen.getByText(/issuer-alpha/)).toHaveTextContent('7')
  expect(screen.getByText(/issuer-alpha/)).not.toHaveTextContent('Mesh defaults · issuer-alpha')
})
test('an override reset confirmation cannot silently follow a parent revision that changed while the dialog was open', async () => {
  const slot = nativeSlot(); slot.inheritance = nativeInheritance('override')
  const execute = vi.fn(async () => ({ result: { ok: true, payload: {} }, confirmed: true }))
  const refresh = vi.fn()
  const view = render(<AccessMeshSlotControls slot={slot} canMutate execute={execute} refresh={refresh} />, { wrapper: I18nProvider })
  await userEvent.click(screen.getByRole('button', { name: 'Use inherited policy' }))
  const replacement = { ...slot, inheritance: { ...slot.inheritance, parent: { ...slot.inheritance.parent!, revision: 99 } } }
  view.rerender(<AccessMeshSlotControls slot={replacement} canMutate execute={execute} refresh={refresh} />)
  await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Use inherited policy' }))
  expect(execute).not.toHaveBeenCalled()
  expect(screen.getByText(en['slots.managed.conflict'])).toBeInTheDocument()
})
test('current-window shortening is rejected in the existing form before any mutation request', async () => {
  const slot = nativeSlot()
  slot.sessionLifecycle = { state: 'active', startedAt: '2026-10-12T05:00:00Z', hardExpiresAt: '2026-10-12T05:23:00Z', rearmAt: null, remainingSeconds: 1000 }
  const execute = vi.fn(async () => ({ result: { ok: true, payload: {} }, confirmed: true }))
  render(<I18nProvider><AccessMeshSlotControls slot={slot} canMutate execute={execute} refresh={vi.fn()} /></I18nProvider>)
  fireEvent.change(screen.getByRole('spinbutton', { name: en['slots.managed.windowDuration'] }), { target: { value: '1' } })
  fireEvent.change(screen.getByRole('combobox', { name: `${en['slots.managed.windowDuration']} · ${en['slots.unit']}` }), { target: { value: 'minutes' } })
  await userEvent.click(screen.getByRole('button', { name: en['slots.managed.saveWindow'] }))
  expect(screen.getByText(en['slots.native.windowExtensionOnly'])).toBeInTheDocument()
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(execute).not.toHaveBeenCalled()
})
