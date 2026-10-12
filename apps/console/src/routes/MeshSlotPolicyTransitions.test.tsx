import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'
import { ConsoleHttpError } from '../api/client'
import type { AccessMeshMutationRequest, ManagedAccessPolicyReadModel, ManagedFleetControlReadModel, PersistentMutationResult } from '../api/models'
import { I18nProvider } from '../i18n/I18nProvider'
import { fleetInstance, meshControl, nativeDefaults, nativeInheritance, nativeSlot } from '../slots/testFixtures'
import { ServerSlots } from './ServerSlots'
import type { MeshAccessPolicyRequest } from './MeshAccessPolicyControls'

function setup(route = '/meshes/mesh-prod/slots', capability = true, failAuthority = false) {
  const state = {
    policy: { durationSeconds: 1380, warningAfterSeconds: 1200, alertAfterSeconds: 1290, rearmAfterSeconds: 180,
      legacyAdmissionEnabled: false, revision: 2, updatedAt: '2026-10-12T05:00:00Z' } as ManagedAccessPolicyReadModel,
    slots: [nativeSlot()], defaults: { ...nativeDefaults(), meshPolicySupported: capability, inheritanceSupported: capability,
      inheritance: nativeInheritance('inherit', 'mesh') },
    failAuthority, conflict: false, loseReceipt: false,
  }
  const control = (id: string): ManagedFleetControlReadModel => ({ ...meshControl(id), policy: structuredClone(state.policy) })
  const loadControl = vi.fn(async (id: string) => {
    if (id === 'alpha' && state.failAuthority) throw new ConsoleHttpError(0, 'request_timeout')
    return control(id)
  })
  const loadSlots = vi.fn(async (id: string) => id === 'alpha' ? structuredClone(state.slots) : [])
  const loadDefaults = vi.fn(async () => structuredClone(state.defaults))
  const mutateMesh = vi.fn(async (id: string, path: string, body: Record<string, unknown>) => {
    expect(id).toBe('alpha'); expect(path).toBe('/actions/fleet/control/policy')
    const request = body as MeshAccessPolicyRequest
    if (state.conflict) {
      state.policy = { ...state.policy, revision: state.policy.revision + 1, durationSeconds: 7200 }
      throw new ConsoleHttpError(409, 'policy_revision_conflict')
    }
    expect(request.expected_revision).toBe(state.policy.revision)
    state.policy = { durationSeconds: request.duration_seconds, warningAfterSeconds: request.warning_after_seconds,
      alertAfterSeconds: request.alert_after_seconds, rearmAfterSeconds: request.rearm_after_seconds,
      legacyAdmissionEnabled: request.legacy_admission_enabled, revision: state.policy.revision + 1, updatedAt: state.policy.updatedAt }
    if (state.loseReceipt) throw new ConsoleHttpError(0, 'request_timeout')
    return { ok: true, control: control('alpha') }
  })
  const mutateNative = vi.fn(async (_id: string, request: AccessMeshMutationRequest): Promise<PersistentMutationResult> => {
    if (request.action !== 'defaults') throw new Error('unexpected native mutation in Mesh test')
    expect(request.expected_revision).toBe(state.defaults.revision)
    state.defaults.revision++
    if (request.inherit_policy) state.defaults.inheritance = nativeInheritance('inherit', 'mesh')
    else {
      state.defaults.inheritance = nativeInheritance('override', 'mesh')
      if (request.policy?.duration_seconds !== undefined) state.defaults.policy.durationSeconds = request.policy.duration_seconds
    }
    return { ok: true, payload: { revision: state.defaults.revision } }
  })
  const props = { instances: [fleetInstance(), fleetInstance('beta')], loadFleetControl: loadControl, loadAccessMeshSlots: loadSlots,
    loadAccessMeshDefaults: loadDefaults, mutateFleetControl: mutateMesh, mutateAccessMesh: mutateNative }
  render(<I18nProvider><MemoryRouter initialEntries={[route]}><Routes>
    <Route path="/meshes/:meshId/slots" element={<ServerSlots {...props} />} />
    <Route path="/servers/:instanceId/slots" element={<ServerSlots {...props} />} />
  </Routes></MemoryRouter></I18nProvider>)
  return { state, loadControl, loadSlots, loadDefaults, mutateMesh, mutateNative }
}
async function editThreeHours() {
  const input = await screen.findByRole('spinbutton', { name: 'Session duration' })
  await waitFor(() => expect(input).toBeEnabled())
  fireEvent.change(input, { target: { value: '3' } })
  fireEvent.change(screen.getByRole('combobox', { name: 'Session duration · Unit' }), { target: { value: 'hours' } })
  return input
}
afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear(); vi.useRealTimers(); vi.restoreAllMocks() })

test('Mesh policy saves three hours through the existing control node and rereads authority and native members', async () => {
  const env = setup()
  await editThreeHours()
  const save = screen.getByRole('button', { name: 'Save' })
  fireEvent.click(save); fireEvent.click(save)
  await waitFor(() => expect(env.mutateMesh).toHaveBeenCalledTimes(1))
  await waitFor(() => expect(env.loadControl.mock.calls.filter(([id]) => id === 'alpha')).toHaveLength(2))
  expect(env.mutateMesh.mock.calls[0][2]).toEqual({ duration_seconds: 10800, warning_after_seconds: 1200, alert_after_seconds: 1290,
    rearm_after_seconds: 180, legacy_admission_enabled: false, expected_revision: 2 })
  expect(env.mutateNative).not.toHaveBeenCalled()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled())
  expect(screen.getByRole('spinbutton', { name: 'Session duration' })).toHaveValue(3)
})

test('old native capability leaves Mesh edits unavailable while readable slots remain visible', async () => {
  const env = setup('/meshes/mesh-prod/slots', false)
  expect(await screen.findByRole('link', { name: 'Alpha' })).toBeInTheDocument()
  expect(await screen.findByText(/does not report the current native slot policy/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
  expect(env.mutateMesh).not.toHaveBeenCalled()
})

test('authority read timeout leaves Mesh settings read-only despite a readable member and its confirmed Production topology', async () => {
  const env = setup('/meshes/mesh-prod/slots', true, true)
  const input = await screen.findByRole('spinbutton', { name: 'Session duration' })
  expect(input).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  expect(env.mutateMesh).not.toHaveBeenCalled()
})

test('Mesh revision conflict rereads and preserves the draft without overwriting the new policy', async () => {
  const env = setup(); env.state.conflict = true
  const input = await editThreeHours()
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(env.state.policy.revision).toBe(3))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled())
  expect(input).toHaveValue(3)
  await userEvent.click(screen.getByRole('button', { name: 'Cancel slot policy edits' }))
  expect(input).toHaveValue(2)
  expect(env.mutateMesh).toHaveBeenCalledTimes(1)
})

test('lost Mesh receipt is confirmed by an exact newer read without automatically resending', async () => {
  const env = setup(); env.state.loseReceipt = true
  await editThreeHours(); await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled())
  expect(screen.getByRole('spinbutton', { name: 'Session duration' })).toHaveValue(3)
  await waitFor(() => expect(env.loadControl.mock.calls.filter(([id]) => id === 'alpha')).toHaveLength(2))
  expect(env.mutateMesh).toHaveBeenCalledTimes(1)
})

test('server settings within Mesh edit local defaults and explicitly reset an override through the native contract', async () => {
  const env = setup('/servers/alpha/slots')
  await editThreeHours(); await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  const reset = await screen.findByRole('button', { name: 'Use inherited policy' })
  expect(env.mutateNative.mock.calls[0][1]).toMatchObject({ action: 'defaults', expected_revision: 2, policy: { duration_seconds: 10800 } })
  await userEvent.click(reset)
  await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Use inherited policy' }))
  await waitFor(() => expect(env.mutateNative).toHaveBeenCalledTimes(2))
  expect(env.mutateNative.mock.calls[1][1]).toMatchObject({ action: 'defaults', expected_revision: 3, inherit_policy: true })
  expect(env.mutateNative.mock.calls[1][1]).not.toHaveProperty('policy')
  expect(env.mutateMesh).not.toHaveBeenCalled()
})


test('fresh native defaults do not keep Mesh policy writable while the authority refresh is pending', async () => {
  vi.useFakeTimers()
  const env = setup()
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  env.loadControl.mockImplementation(async (id) => id === 'alpha'
    ? new Promise<ManagedFleetControlReadModel>(() => undefined)
    : { ...meshControl(id), policy: env.state.policy })
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
  expect(env.loadControl.mock.calls.filter(([id]) => id === 'alpha')).toHaveLength(2)
  expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  // A 20-second request must not be superseded forever by a 15-second poll.
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
  expect(env.loadControl.mock.calls.filter(([id]) => id === 'alpha')).toHaveLength(2)
  expect(env.mutateMesh).not.toHaveBeenCalled()
})
