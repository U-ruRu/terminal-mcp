import { cleanup,fireEvent,render,screen,waitFor,within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter,Route,Routes } from 'react-router-dom'
import { afterEach,expect,test,vi } from 'vitest'

import { ConsoleHttpError } from '../api/client'
import type { AccessMeshMutationRequest,AccessMutationResult } from '../api/models'
import { I18nProvider } from '../i18n/I18nProvider'
import { fleetInstance,nativeDefaults,nativeInheritance,nativeSlot,standaloneControl } from '../slots/testFixtures'
import { ServerSlots,type ServerSlotsProps } from './ServerSlots'

function setup(route = '/slots', overrides: Partial<ServerSlotsProps> = {}) {
  const state = { slots: [nativeSlot()], defaults: nativeDefaults() }
  const loadSlots = vi.fn(async () => structuredClone(state.slots))
  const loadDefaults = vi.fn(async () => structuredClone(state.defaults))
  const loadControl = vi.fn(async (id: string) => standaloneControl(id))
  const mutate = vi.fn(async (_id: string, request: AccessMeshMutationRequest): Promise<AccessMutationResult> => {
    if (request.action === 'defaults') {
      state.defaults.revision++
      if (request.policy?.duration_seconds !== undefined) state.defaults.policy.durationSeconds = request.policy.duration_seconds
      if (request.policy?.rearm_enabled !== undefined) state.defaults.policy.rearmEnabled = request.policy.rearm_enabled
      if (request.legacy_admission_enabled !== undefined) state.defaults.legacyAdmissionEnabled = request.legacy_admission_enabled
      return { ok: true, payload: { revision: state.defaults.revision } }
    }
    if (request.action === 'delete') {
      state.slots = state.slots.filter((slot) => slot.slotId !== request.slot_id)
      return { ok: true, payload: { revision: request.expected_revision + 1 } }
    }
    if (request.action !== 'policy') throw new Error('unexpected mutation in policy fixture')
    const slot = state.slots.find((item) => item.slotId === request.slot_id)!
    if (request.expected_revision !== slot.revision) return { ok: false, code: 'access_mesh_revision_conflict', payload: { outcome: 'not_committed' } }
    slot.revision++
    if (request.inherit_policy) {
      slot.policy = structuredClone(state.defaults.policy)
      slot.inheritance = nativeInheritance()
    } else {
      if (request.policy?.duration_seconds !== undefined) slot.policy.durationSeconds = request.policy.duration_seconds
      slot.inheritance = nativeInheritance('override')
    }
    return { ok: true, payload: { revision: slot.revision } }
  })
  const props: ServerSlotsProps = { instances: [fleetInstance()], loadAccessMeshSlots: loadSlots, loadAccessMeshDefaults: loadDefaults, loadFleetControl: loadControl, mutateAccessMesh: mutate, ...overrides }
  const view = render(<I18nProvider><MemoryRouter initialEntries={[route]}><Routes>
    {['/slots', '/servers/:instanceId/slots', '/servers/:instanceId/slots/:slotId'].map((path) => <Route key={path} path={path} element={<ServerSlots {...props} />} />)}
  </Routes></MemoryRouter></I18nProvider>)
  return { state, props, loadSlots, loadDefaults, loadControl, mutate, view }
}
const detail = '/servers/alpha/slots/as_alpha?issuer=alpha&slot=as_alpha'

afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear(); vi.restoreAllMocks() })

test('a denied native slot read prevents slot creation', async () => {
  setup('/slots', { loadAccessMeshSlots: vi.fn(async () => {
    throw new ConsoleHttpError(403, 'insufficient_scope')
  }) })

  expect(await screen.findByText(/restricted for this connection/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Create slot' })).toBeDisabled()
})

test('a failed refresh retains last observed rows but disables mutations', async () => {
  const test = setup()
  await screen.findByRole('link', { name: 'Alpha' })
  test.loadSlots.mockRejectedValue(new Error('offline'))
  await userEvent.click(screen.getByRole('button', { name: 'Refresh slots' }))
  await screen.findByText(/Retained rows may be stale/)
  expect(screen.getByRole('link', { name: 'Alpha' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Pause' })).toBeDisabled()
})

test('saving a slot policy addresses that slot with its current revision', async () => {
  const env = setup(detail)
  const input = await screen.findByRole('spinbutton', { name: 'Slot session duration' })
  fireEvent.change(input, { target: { value: '3' } })
  fireEvent.change(screen.getByRole('combobox', { name: 'Slot session duration · Unit' }), { target: { value: 'hours' } })

  await userEvent.click(screen.getByRole('button', { name: 'Save slot policy' }))

  await waitFor(() => expect(env.mutate).toHaveBeenCalledWith('alpha', expect.objectContaining({
    action: 'policy', slot_id: 'as_alpha', expected_revision: 7, policy: expect.objectContaining({ duration_seconds: 10800 }),
  })))
})

test('confirming inheritance binds both the slot and its parent revisions', async () => {
  const env = setup(detail)
  env.state.slots[0].inheritance = nativeInheritance('override')
  await userEvent.click(await screen.findByRole('button', { name: 'Use inherited policy' }))
  const dialog = screen.getByRole('dialog')

  await userEvent.click(within(dialog).getByRole('button', { name: 'Use inherited policy' }))

  await waitFor(() => expect(env.mutate).toHaveBeenCalledWith('alpha', expect.objectContaining({
    action: 'policy', inherit_policy: true, expected_parent_revision: 2, slot_id: 'as_alpha', expected_revision: 7,
  })))
})

test('a stale slot revision preserves the unsaved draft', async () => {
  const env = setup(detail)
  const input = await screen.findByRole('spinbutton', { name: 'Slot session duration' })
  fireEvent.change(input, { target: { value: '50' } })
  env.state.slots[0].revision++

  await userEvent.click(screen.getByRole('button', { name: 'Save slot policy' }))

  await waitFor(() => expect(env.loadSlots).toHaveBeenCalledTimes(2))
  expect(input).toHaveValue(50)
  expect(screen.getByRole('button', { name: 'Save slot policy' })).toBeDisabled()
})

test('saving Standalone policy sends its admission and Rearm settings with the defaults revision', async () => {
  const env = setup('/servers/alpha/slots')
  const input = await screen.findByRole('spinbutton', { name: 'Session duration' })
  fireEvent.change(input, { target: { value: '3' } })
  fireEvent.change(screen.getByRole('combobox', { name: 'Session duration · Unit' }), { target: { value: 'hours' } })
  await userEvent.click(screen.getByRole('checkbox', { name: 'Legacy admission' }))
  await userEvent.click(screen.getByRole('checkbox', { name: 'Rearm' }))

  await userEvent.click(screen.getByRole('button', { name: 'Save' }))

  await waitFor(() => expect(env.mutate).toHaveBeenCalledWith('alpha', expect.objectContaining({
    action: 'defaults', expected_revision: 2, legacy_admission_enabled: true,
    policy: expect.objectContaining({ duration_seconds: 10800, rearm_enabled: false }),
  })))
})

test('confirmed deletion removes the slot from every displayed node snapshot', async () => {
  setup('/slots', { instances: [fleetInstance(), fleetInstance('beta')] })
  await screen.findByRole('link', { name: 'Alpha' })
  await userEvent.click(screen.getByLabelText('More actions'))
  await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
  await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }))

  await waitFor(() => expect(screen.queryByRole('link', { name: 'Alpha' })).not.toBeInTheDocument())
})

test('read-only pairing can display fresh slots while all native writes remain disabled', async () => {
  const instance = fleetInstance(); instance.profile.metadata.scope = 'terminal:read'
  const env = setup('/servers/alpha/slots', { instances: [instance] })
  expect(await screen.findByRole('link', { name: 'Alpha' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Pause' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Create slot' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  expect(env.mutate).not.toHaveBeenCalled()
})
