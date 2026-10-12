import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Link, MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'

import { ConsoleHttpError } from '../api/client'
import { ConsoleOperationError } from '../api/slotContracts'
import type { AccessMeshMutationRequest, PersistentMutationResult } from '../api/models'
import { I18nProvider } from '../i18n/I18nProvider'
import { nativeDefaults, nativeInheritance, nativeSlot, fleetInstance, standaloneControl } from '../slots/testFixtures'
import { ServerSlots, type ServerSlotsProps } from './ServerSlots'

function useRouteProbe() { const location = useLocation(); return location.pathname + location.search }
function Probe() { const path = useRouteProbe(); const navigate = useNavigate(); return <><output data-testid="path">{path}</output><button onClick={() => navigate(-1)}>Back</button><Link to="/slots">All slots</Link></> }
function setup(route = '/slots', overrides: Partial<ServerSlotsProps> = {}) {
  const state = { slots: [nativeSlot()], defaults: nativeDefaults() }
  const loadSlots = vi.fn(async () => structuredClone(state.slots))
  const loadDefaults = vi.fn(async () => structuredClone(state.defaults))
  const loadControl = vi.fn(async (id: string) => standaloneControl(id))
  const mutate = vi.fn(async (_id: string, request: AccessMeshMutationRequest): Promise<PersistentMutationResult> => {
    if (request.action === 'create') {
      const slot = nativeSlot('new'); slot.issuerNodeId = 'alpha'; slot.revision = 1; slot.codeGeneration = 1; state.slots.push(slot)
      return { ok: true, payload: { issuer_node_id: slot.issuerNodeId, slot_id: slot.slotId, logical_agent_id: slot.logicalAgentId, public_name: slot.publicName, revision: slot.revision, code_generation: 1, code_expires_at: null, access_code: '0042' } }
    }
    if (request.action === 'defaults') {
      state.defaults.revision++
      if (request.policy?.duration_seconds !== undefined) state.defaults.policy.durationSeconds = request.policy.duration_seconds
      return { ok: true, payload: { revision: state.defaults.revision } }
    }
    const slot = state.slots.find((item) => item.slotId === request.slot_id)!
    if (request.expected_revision !== slot.revision) return { ok: false, code: 'access_mesh_revision_conflict', payload: { outcome: 'not_committed' } }
    slot.revision++
    if (request.action === 'rotate') { slot.codeGeneration = (slot.codeGeneration ?? 0) + 1; return { ok: true, payload: { issuer_node_id: slot.issuerNodeId, slot_id: slot.slotId, logical_agent_id: slot.logicalAgentId, public_name: slot.publicName, revision: slot.revision, code_generation: slot.codeGeneration, code_expires_at: null, access_code: '0731' } } }
    if (request.action === 'delete') slot.state = 'deleted'
    if (request.action === 'suspend' || request.action === 'resume') { slot.state = request.action === 'suspend' ? 'suspended' : 'active'; slot.sessionLifecycle.state = request.action === 'suspend' ? 'suspended' : 'idle' }
    if (request.action === 'policy' && request.policy) {
      const p = request.policy
      if (p.duration_seconds !== undefined) slot.policy.durationSeconds = p.duration_seconds
      if (p.cooldown_seconds !== undefined) slot.policy.cooldownSeconds = p.cooldown_seconds
      if (p.warning_seconds !== undefined) slot.policy.warningSeconds = p.warning_seconds
      if (p.draining_seconds !== undefined) slot.policy.drainingSeconds = p.draining_seconds
      if (p.rearm_enabled !== undefined) slot.policy.rearmEnabled = p.rearm_enabled
      if (p.release_on_end !== undefined) slot.policy.releaseOnEnd = p.release_on_end
      slot.inheritance = nativeInheritance('override')
    }
    if (request.action === 'policy' && request.inherit_policy) { slot.policy = structuredClone(state.defaults.policy); slot.inheritance = nativeInheritance() }
    if (request.action === 'deadline') slot.sessionLifecycle.hardExpiresAt = request.deadline_at
    return { ok: true, payload: { revision: slot.revision } }
  })
  const props: ServerSlotsProps = { instances: [fleetInstance()], loadAccessMeshSlots: loadSlots, loadAccessMeshDefaults: loadDefaults, loadFleetControl: loadControl, mutateAccessMesh: mutate, ...overrides }
  const view = render(<I18nProvider><MemoryRouter initialEntries={[route]}><Probe /><Routes>
    {['/slots', '/slots/:logicalAgentId', '/servers/:instanceId/slots', '/servers/:instanceId/slots/:logicalAgentId', '/meshes/:meshId/slots', '/meshes/:meshId/slots/:logicalAgentId'].map((path) => <Route key={path} path={path} element={<ServerSlots {...props} />} />)}
  </Routes></MemoryRouter></I18nProvider>)
  return { state, props, loadSlots, loadDefaults, loadControl, mutate, view }
}
const detail = '/servers/alpha/slots/la_alpha?issuer=alpha&slot=as_alpha'

afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear(); vi.restoreAllMocks() })

test('native slots load without a legacy persistent snapshot or realtime gate', async () => {
  const legacy = vi.fn()
  setup('/slots', { mutatePersistent: legacy, loadManagedSlotStatus: legacy, loadSlotAudit: legacy })
  expect(await screen.findByRole('link', { name: 'Alpha' })).toBeInTheDocument()
  expect(document.querySelector('.slot-card-row')).toBeInTheDocument()
  expect(legacy).not.toHaveBeenCalled()
})

test('the old endpoint is explicitly unsupported and never becomes an empty or legacy slot list', async () => {
  const unavailable = vi.fn(async () => { throw new ConsoleHttpError(404, 'not_found') })
  setup('/slots', { loadAccessMeshSlots: unavailable, loadAccessMeshDefaults: unavailable })
  expect(await screen.findByText(/does not support the current Access slot interface/)).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Alpha' })).not.toBeInTheDocument()
  expect(document.querySelector('.slot-create')).toBeNull()
  expect(document.querySelector('.feedback-empty')).toBeNull()
})

test.each([
  [new ConsoleOperationError('access_mesh_disabled'), /disabled/i],
  [new ConsoleHttpError(403, 'insufficient_scope'), /restricted for this connection/],
  [new Error('network_error'), /could not be loaded/],
])('a failed native read has an explicit state instead of an empty list: %s', async (error, text) => {
  setup('/slots', { loadAccessMeshSlots: vi.fn(async () => { throw error }) })
  expect(await screen.findByText(text)).toBeInTheDocument()
  expect(document.querySelector('.slot-card')).toBeNull()
  expect(screen.getByRole('button', { name: 'Create slot' })).toBeDisabled()
})

test('slots remain visible when defaults are denied', async () => {
  setup('/slots', { loadAccessMeshDefaults: vi.fn(async () => { throw new ConsoleHttpError(403, 'insufficient_scope') }) })
  expect(await screen.findByRole('link', { name: 'Alpha' })).toBeInTheDocument()
  expect(screen.getByText(/restricted for this connection/)).toBeInTheDocument()
  expect(document.querySelector('.slot-create')).toBeNull()
})

test('topology failure keeps native slots visible and leaves parent ownership unknown', async () => {
  const defaults = nativeDefaults(); delete defaults.writeTarget; delete defaults.inheritance
  setup('/slots', { loadFleetControl: vi.fn(async () => { throw new Error('unavailable') }), loadAccessMeshDefaults: vi.fn(async () => defaults) })
  await screen.findByRole('link', { name: 'Alpha' })
  expect(screen.getByRole('combobox', { name: 'Filter' })).toHaveValue('all')
  expect(screen.getAllByRole('option', { name: /Alpha server.*Unknown/ }).length).toBeGreaterThan(0)
  expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
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

test('Slot Details contains exactly six native policy inputs and no managed-only or legacy timing controls', async () => {
  const old = vi.fn()
  setup(detail, { mutatePersistent: old, loadSlotAudit: old, loadManagedSlotStatus: old })
  await screen.findByRole('spinbutton', { name: 'Slot session duration' })
  expect(screen.getAllByRole('spinbutton')).toHaveLength(4)
  expect(screen.getByRole('checkbox', { name: 'Reopen automatically after waiting' })).toBeInTheDocument()
  expect(screen.getByRole('combobox', { name: 'Release claims when the session ends' })).toBeInTheDocument()
  expect(screen.queryByLabelText(/Alert after/)).not.toBeInTheDocument()
  expect(old).not.toHaveBeenCalled()
})

test('three hours are saved as 10800 seconds for only the selected slot and confirmed by a reread', async () => {
  const test = setup(detail)
  const other = nativeSlot('beta'); test.state.slots.push(other)
  const input = await screen.findByRole('spinbutton', { name: 'Slot session duration' })
  fireEvent.change(input, { target: { value: '3' } })
  fireEvent.change(screen.getByRole('combobox', { name: 'Slot session duration · Unit' }), { target: { value: 'hours' } })
  await userEvent.click(screen.getByRole('button', { name: 'Save slot policy' }))
  await waitFor(() => expect(test.loadSlots).toHaveBeenCalledTimes(2))
  expect(test.mutate).toHaveBeenCalledTimes(1)
  expect(test.mutate.mock.calls[0][1]).toMatchObject({ action: 'policy', slot_id: 'as_alpha', expected_revision: 7, policy: { duration_seconds: 10800 } })
  expect(other.policy.durationSeconds).toBe(1380)
  expect(input).toHaveValue(3)
  expect(screen.getByText(/Slot override/)).toBeInTheDocument()
})

test('return to inheritance is an explicit revision-bound operation and Cancel writes nothing', async () => {
  const test = setup(detail)
  test.state.slots[0].inheritance = nativeInheritance('override')
  const reset = await screen.findByRole('button', { name: 'Use inherited policy' })
  await userEvent.click(reset)
  await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
  expect(test.mutate).not.toHaveBeenCalled()
  await userEvent.click(reset)
  await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Use inherited policy' }))
  await waitFor(() => expect(test.loadSlots).toHaveBeenCalledTimes(2))
  expect(test.mutate.mock.calls[0][1]).toMatchObject({ action: 'policy', inherit_policy: true, slot_id: 'as_alpha', expected_revision: 7 })
  expect(test.mutate.mock.calls[0][1]).not.toHaveProperty('policy')
})

test('a stale slot revision preserves the draft and blocks another save until Cancel', async () => {
  const test = setup(detail)
  const input = await screen.findByRole('spinbutton', { name: 'Slot session duration' })
  fireEvent.change(input, { target: { value: '50' } })
  test.state.slots[0].revision++
  await userEvent.click(screen.getByRole('button', { name: 'Save slot policy' }))
  await waitFor(() => expect(test.loadSlots).toHaveBeenCalledTimes(2))
  expect(input).toHaveValue(50)
  expect(screen.getByRole('button', { name: 'Save slot policy' })).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: 'Cancel slot policy edits' }))
  expect(input).toHaveValue(23)
  expect(screen.getByRole('button', { name: 'Save slot policy' })).toBeEnabled()
})

test('Standalone policy is saved through native defaults with its own revision', async () => {
  const test = setup('/servers/alpha/slots')
  const input = await screen.findByRole('spinbutton', { name: 'Session duration' })
  fireEvent.change(input, { target: { value: '3' } })
  fireEvent.change(screen.getByRole('combobox', { name: 'Session duration · Unit' }), { target: { value: 'hours' } })
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(test.loadDefaults).toHaveBeenCalledTimes(2))
  expect(test.mutate.mock.calls[0][1]).toMatchObject({ action: 'defaults', expected_revision: 2, policy: { duration_seconds: 10800 } })
  expect(test.mutate.mock.calls[0][1]).toHaveProperty('legacy_enabled', false)
})


test('native Standalone defaults remain editable when the optional fleet-control route is unsupported', async () => {
  const defaults = { ...nativeDefaults(), inheritanceSupported: true, meshPolicySupported: false }
  const env = setup('/servers/alpha/slots', {
    loadFleetControl: vi.fn(async () => { throw new ConsoleHttpError(404, 'not_found') }),
    loadAccessMeshDefaults: vi.fn(async () => structuredClone(defaults)),
  })
  const input = await screen.findByRole('spinbutton', { name: 'Session duration' })
  await waitFor(() => expect(input).toBeEnabled())
  expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  expect(env.mutate).not.toHaveBeenCalled()
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


test('old native semantics show slots and codes but never enable a next-session policy write', async () => {
  const slot = nativeSlot(); slot.inheritanceSupported = undefined
  const env = setup(detail, { loadAccessMeshSlots: vi.fn(async () => [structuredClone(slot)]) })
  const input = await screen.findByRole('spinbutton', { name: 'Slot session duration' })
  expect(input).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Save slot policy' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Rotate Access code' })).toBeEnabled()
  expect(screen.getByText(/Update this server to edit next-session policies safely/)).toBeInTheDocument()
  expect(env.mutate).not.toHaveBeenCalled()
})
