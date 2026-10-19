import { cleanup,fireEvent,render,screen,within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach,expect,test,vi } from 'vitest'
import { I18nProvider } from '../i18n/I18nProvider'
import { en } from '../i18n/catalogs'
import { nativeInheritance,nativeSlot } from '../slots/testFixtures'
import { AccessMeshSlotControls } from './AccessMeshPolicyControls'

afterEach(cleanup)

test('an inherited-policy confirmation sends the reset intent after a background parent revision change', async () => {
  const slot = nativeSlot(); slot.inheritance = nativeInheritance('override')
  const execute = vi.fn(async () => ({ result: { ok: true, payload: {} }, confirmed: true }))
  const refresh = vi.fn()
  const notify = vi.fn()
  const view = render(<AccessMeshSlotControls slot={slot} canMutate execute={execute} refresh={refresh} notify={notify} />, { wrapper: I18nProvider })
  await userEvent.click(screen.getByRole('button', { name: 'Use inherited policy' }))
  const replacement = { ...slot, inheritance: { ...slot.inheritance, parent: { ...slot.inheritance.parent!, revision: 99 } } }
  view.rerender(<AccessMeshSlotControls slot={replacement} canMutate execute={execute} refresh={refresh} notify={notify} />)
  await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Use inherited policy' }))
  expect(execute).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ action: 'policy', slot_id: slot.slotId, inherit_policy: true, expected_parent_revision: 99 }))
  expect(notify).not.toHaveBeenCalled()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
test('current-window shortening is rejected in the existing form before any mutation request', async () => {
  const slot = nativeSlot()
  slot.sessionLifecycle = { state: 'active', startedAt: '2026-10-12T05:00:00Z', hardExpiresAt: '2026-10-12T05:23:00Z', rearmAt: null, remainingSeconds: 1000 }
  const execute = vi.fn(async () => ({ result: { ok: true, payload: {} }, confirmed: true }))
  const notify = vi.fn()
  render(<I18nProvider><AccessMeshSlotControls slot={slot} canMutate execute={execute} refresh={vi.fn()} notify={notify} /></I18nProvider>)
  fireEvent.change(screen.getByRole('spinbutton', { name: en['slots.managed.windowDuration'] }), { target: { value: '1' } })
  fireEvent.change(screen.getByRole('combobox', { name: `${en['slots.managed.windowDuration']} · ${en['slots.unit']}` }), { target: { value: 'minutes' } })
  await userEvent.click(screen.getByRole('button', { name: en['slots.managed.saveWindow'] }))
  expect(notify).toHaveBeenCalledExactlyOnceWith(en['slots.native.windowExtensionOnly'])
  expect(screen.queryByText(en['slots.native.windowExtensionOnly'])).not.toBeInTheDocument()
  expect(execute).not.toHaveBeenCalled()
})
