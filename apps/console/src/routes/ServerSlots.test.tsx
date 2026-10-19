import { act,cleanup,fireEvent,render,screen,waitFor,within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter,Route,Routes } from 'react-router-dom'
import { afterEach,expect,test,vi } from 'vitest'

import { ConsoleHttpError } from '../api/client'
import type { AccessMeshMutationRequest,AccessMutationResult } from '../api/models'
import { I18nProvider } from '../i18n/I18nProvider'
import { saveCachedFleetControl } from '../connections/controlState'
import { fleetInstance,meshControl,nativeDefaults,nativeInheritance,nativeSlot,standaloneControl } from '../slots/testFixtures'
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
      if (request.policy?.warning_seconds !== undefined) slot.policy.warningSeconds = request.policy.warning_seconds
      slot.inheritance = nativeInheritance('override')
    }
    return { ok: true, payload: { revision: slot.revision } }
  })
  const props: ServerSlotsProps = { instances: [fleetInstance()], loadAccessMeshSlots: loadSlots, loadAccessMeshDefaults: loadDefaults, loadFleetControl: loadControl, mutateAccessMesh: mutate, ...overrides }
  const element = (current: ServerSlotsProps) => <I18nProvider><MemoryRouter initialEntries={[route]}><Routes>
    {['/slots', '/servers/:instanceId/slots', '/servers/:instanceId/slots/:slotId', '/meshes/:meshId/slots/:slotId'].map((path) => <Route key={path} path={path} element={<ServerSlots {...current} />} />)}
  </Routes></MemoryRouter></I18nProvider>
  const view = render(element(props))
  return { state, props, loadSlots, loadDefaults, loadControl, mutate, view, rerender: (next: ServerSlotsProps) => view.rerender(element(next)) }
}
const detail = '/servers/alpha/slots/as_alpha?issuer=alpha&slot=as_alpha'

afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear(); vi.useRealTimers(); vi.restoreAllMocks() })

test('a denied native slot read prevents slot creation', async () => {
  setup('/slots', { loadAccessMeshSlots: vi.fn(async () => {
    throw new ConsoleHttpError(403, 'insufficient_scope')
  }) })

  expect(await screen.findByText(/restricted for this connection/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Create slot' })).toBeDisabled()
})

test('a failed list refresh retains known rows and available actions', async () => {
  const test = setup()
  await screen.findByRole('link', { name: 'Alpha' })
  test.loadSlots.mockRejectedValue(new Error('offline'))
  await userEvent.click(screen.getByRole('button', { name: 'Refresh slots' }))
  await screen.findByText('offline')
  expect(screen.getByRole('link', { name: 'Alpha' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled()
  expect(screen.queryByText(/Retained rows may be stale/)).not.toBeInTheDocument()
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

test.each(['standalone', 'mesh'])('confirming %s inheritance uses current slot and native defaults revisions', async (kind) => {
  const env = kind === 'mesh'
    ? setup('/meshes/mesh-prod/slots/as_alpha?issuer=alpha&slot=as_alpha', { loadFleetControl: async (id) => meshControl(id) })
    : setup(detail)
  if (kind === 'mesh') env.state.defaults = { ...env.state.defaults, writeTarget: 'fleet_control', inheritance: nativeInheritance('inherit', 'mesh') }
  env.state.slots[0].inheritance = nativeInheritance('override')
  await userEvent.click(await screen.findByRole('button', { name: 'Use inherited policy' }))
  const dialog = screen.getByRole('dialog')
  env.state.slots[0].revision = 8
  env.state.defaults.revision = 9
  if (env.state.defaults.inheritance?.parent) env.state.defaults.inheritance.parent.revision = 20

  await userEvent.click(within(dialog).getByRole('button', { name: 'Use inherited policy' }))

  await waitFor(() => expect(env.mutate).toHaveBeenCalledWith('alpha', expect.objectContaining({
    action: 'policy', inherit_policy: true, expected_parent_revision: 9, slot_id: 'as_alpha', expected_revision: 8,
  })))
})

test.each(['external edit', 'CAS race'])('saving after a slot %s applies the full captured draft with the latest revision', async (change) => {
  const env = setup(detail)
  const input = await screen.findByRole('spinbutton', { name: 'Slot session duration' })
  fireEvent.change(input, { target: { value: '50' } })
  env.state.slots[0].revision++
  env.state.slots[0].policy.warningSeconds = 240
  if (change === 'CAS race') env.mutate.mockImplementationOnce(async () => {
    env.state.slots[0].revision++
    return { ok: false, code: 'revision_conflict', payload: { outcome: 'not_committed' } }
  })

  await userEvent.click(screen.getByRole('button', { name: 'Save slot policy' }))

  await waitFor(() => expect(env.mutate).toHaveBeenCalledWith('alpha', expect.objectContaining({
    action: 'policy', expected_revision: change === 'CAS race' ? 9 : 8, policy: {
      duration_seconds: 3000, warning_seconds: 180, draining_seconds: 90,
      cooldown_seconds: 180, rearm_enabled: true, release_on_end: null,
    },
  })))
  await screen.findByText('Server values confirmed.')
  expect(input).toHaveValue(50)
  expect(screen.getByRole('button', { name: 'Save slot policy' })).toBeEnabled()
  expect(env.mutate).toHaveBeenCalledTimes(change === 'CAS race' ? 2 : 1)
  if (change === 'CAS race') expect(env.mutate.mock.calls[0][1].idempotency_key).not.toBe(env.mutate.mock.calls[1][1].idempotency_key)
})

test('saving Standalone policy sends its admission and Rearm settings with the defaults revision', async () => {
  const env = setup('/servers/alpha/slots')
  const input = await screen.findByRole('spinbutton', { name: 'Session duration' })
  fireEvent.change(input, { target: { value: '3' } })
  fireEvent.change(screen.getByRole('combobox', { name: 'Session duration · Unit' }), { target: { value: 'hours' } })
  await userEvent.click(screen.getByRole('checkbox', { name: 'Legacy admission' }))
  await userEvent.click(screen.getByRole('checkbox', { name: 'Rearm' }))
  env.state.defaults.revision = 5

  await userEvent.click(screen.getByRole('button', { name: 'Save' }))

  await waitFor(() => expect(env.mutate).toHaveBeenCalledWith('alpha', expect.objectContaining({
    action: 'defaults', expected_revision: 5, legacy_admission_enabled: true,
    policy: expect.objectContaining({ duration_seconds: 10800, rearm_enabled: false }),
  })))
})

test('confirmed deletion removes only the selected registry row', async () => {
  let alphaSlots = [nativeSlot()]
  setup('/slots?filter=all', {
    instances: [fleetInstance(), fleetInstance('beta')],
    loadAccessMeshSlots: async (id) => structuredClone(id === 'alpha' ? alphaSlots : [nativeSlot()]),
    mutateAccessMesh: async (_id, request) => {
      if (request.action !== 'delete') throw new Error('unexpected fixture action')
      alphaSlots = []
      return { ok: true, payload: { revision: request.expected_revision + 1 } }
    },
  })
  await waitFor(() => expect(screen.getAllByRole('link', { name: 'Alpha' })).toHaveLength(2))
  const card = screen.getAllByRole('link', { name: 'Alpha' })[0].closest('article')!
  await userEvent.click(within(card).getByLabelText('More actions'))
  await userEvent.click(within(card).getByRole('button', { name: 'Delete' }))
  await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }))

  await waitFor(() => expect(screen.getAllByRole('link', { name: 'Alpha' })).toHaveLength(1))
  expect(screen.getByRole('link', { name: 'Alpha' })).toHaveAttribute('href', expect.stringContaining('owner=server%3Abeta'))
})

test('scope choices share one Mesh registry, retain actual Standalone and omit unavailable profiles', async () => {
  const shared = (id: string) => {
    const control = meshControl(id)
    control.nodes.push({ ...control.nodes[1], nodeId: 'gamma', origin: 'https://gamma.example' })
    return control
  }
  const secondary = fleetInstance('secondary')
  saveCachedFleetControl('secondary', standaloneControl('secondary'), Date.now())
  const loadSlots = vi.fn(async (id: string) => [nativeSlot(id)])
  const loadDefaults = vi.fn(async () => nativeDefaults())
  setup('/slots?filter=all', {
    instances: [fleetInstance('gamma'), fleetInstance('beta'), fleetInstance(), secondary],
    loadFleetControl: async (id) => {
      if (id === 'secondary') throw new ConsoleHttpError(0, 'request_timeout')
      return id === 'beta' ? standaloneControl(id) : shared(id)
    },
    loadAccessMeshSlots: loadSlots, loadAccessMeshDefaults: loadDefaults,
  })

  await waitFor(() => expect(loadSlots).toHaveBeenCalledTimes(2))

  expect(loadSlots.mock.calls.map(([id]) => id).sort()).toEqual(['alpha', 'beta'])
  expect(loadDefaults).toHaveBeenCalledTimes(2)
  const scopePicker = screen.getByRole('combobox', { name: 'Filter' })
  expect(within(scopePicker).getAllByRole('option').map((option) => option.textContent)).toEqual(['All', 'Beta server', 'Production'])
  expect(screen.queryByRole('combobox', { name: 'Create on server' })).not.toBeInTheDocument()
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

test.each([
  { kind: 'standalone', trigger: 'status and callback change' },
  { kind: 'mesh', trigger: 'elapsed list polling' },
])('a $trigger retains the selected $kind policy form without reloading control or defaults', async ({ kind, trigger }) => {
  if (trigger === 'elapsed list polling') vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
  const original = fleetInstance()
  const scope = kind === 'mesh' ? 'mesh:mesh-prod' : 'server:alpha'
  const loadControl = vi.fn(async (id: string) => kind === 'mesh' ? { ...meshControl(id), policy: {
    durationSeconds: 1380, warningAfterSeconds: 1200, alertAfterSeconds: 1290, rearmAfterSeconds: 180,
    rearmEnabled: true, legacyAdmissionEnabled: false, revision: 2, updatedAt: '2026-10-12T05:00:00Z',
  } } : standaloneControl(id))
  const env = setup('/slots?filter=' + scope, { instances: [original, fleetInstance('beta')],
    loadFleetControl: loadControl, mutateFleetControl: async () => ({ ok: true }) })
  if (kind === 'mesh') env.state.defaults = { ...env.state.defaults, writeTarget: 'fleet_control',
    meshPolicySupported: true, inheritance: nativeInheritance('inherit', 'mesh') }
  const input = await screen.findByRole('spinbutton', { name: 'Session duration' })
  await waitFor(() => expect(loadControl).toHaveBeenCalledTimes(2))
  fireEvent.change(input, { target: { value: '50' } })
  input.focus()
  const controlReads = loadControl.mock.calls.length
  const defaultsReads = env.loadDefaults.mock.calls.length
  const slotReads = env.loadSlots.mock.calls.length

  if (trigger === 'status and callback change') {
    env.rerender({ ...env.props, instances: [original, { ...fleetInstance('beta'), runtime: { ...fleetInstance('beta').runtime, authStatus: 'error' } }],
      loadFleetControl: (id) => loadControl(id) })
    await act(async () => undefined)
  } else {
    await act(async () => { await vi.advanceTimersByTimeAsync(31_000) })
    expect(env.loadSlots.mock.calls.length).toBeGreaterThan(slotReads)
  }

  expect(screen.getByRole('spinbutton', { name: 'Session duration' })).toBe(input)
  expect(input).toHaveValue(50)
  expect(input).toHaveFocus()
  expect(screen.getByRole('combobox', { name: 'Filter' })).toHaveValue(scope)
  expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  expect(loadControl).toHaveBeenCalledTimes(controlReads)
  expect(env.loadDefaults).toHaveBeenCalledTimes(defaultsReads)
})
