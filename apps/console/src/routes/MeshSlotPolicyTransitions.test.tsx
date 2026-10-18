import { cleanup,fireEvent,render,screen,waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter,Route,Routes } from 'react-router-dom'
import { afterEach,expect,test,vi } from 'vitest'
import { ConsoleHttpError } from '../api/client'
import type { AccessMeshMutationRequest,ManagedAccessPolicyReadModel,ManagedFleetControlReadModel,AccessMutationResult } from '../api/models'
import { I18nProvider } from '../i18n/I18nProvider'
import { fleetInstance,meshControl,nativeDefaults,nativeInheritance,nativeSlot } from '../slots/testFixtures'
import type { MeshAccessPolicyRequest } from './MeshAccessPolicyControls'
import { ServerSlots } from './ServerSlots'

function setup(route = '/meshes/mesh-prod/slots', capability = true, failAuthority = false) {
  const state = {
    policy: { durationSeconds: 1380, warningAfterSeconds: 1200, alertAfterSeconds: 1290, rearmAfterSeconds: 180,
      rearmEnabled: true, legacyAdmissionEnabled: false,
      revision: 2, updatedAt: '2026-10-12T05:00:00Z' } as ManagedAccessPolicyReadModel,
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
  const mutateMesh = vi.fn(async (_id: string, _path: string, body: Record<string, unknown>) => {
    const request = body as MeshAccessPolicyRequest
    if (state.conflict) {
      state.policy = { ...state.policy, revision: state.policy.revision + 1, durationSeconds: 7200 }
      throw new ConsoleHttpError(409, 'policy_revision_conflict')
    }
    state.policy = { durationSeconds: request.duration_seconds, warningAfterSeconds: request.warning_after_seconds,
      alertAfterSeconds: request.alert_after_seconds, rearmAfterSeconds: request.rearm_after_seconds,
      rearmEnabled: request.rearm_enabled, legacyAdmissionEnabled: request.legacy_admission_enabled,
      revision: state.policy.revision + 1, updatedAt: state.policy.updatedAt }
    if (state.loseReceipt) throw new ConsoleHttpError(0, 'request_timeout')
    return { ok: true, control: control('alpha') }
  })
  const mutateNative = vi.fn<(id: string, request: AccessMeshMutationRequest) => Promise<AccessMutationResult>>(async () => ({ ok: true, payload: {} }))
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

test('saving Mesh policy sends its admission and Rearm settings with the control revision', async () => {
  const env = setup()
  await editThreeHours()
  await userEvent.click(screen.getByRole('checkbox', { name: 'Legacy admission' }))
  await userEvent.click(screen.getByRole('checkbox', { name: 'Rearm' }))

  await userEvent.click(screen.getByRole('button', { name: 'Save' }))

  await waitFor(() => expect(env.mutateMesh).toHaveBeenCalledWith('alpha', '/actions/fleet/control/policy', {
    duration_seconds: 10800, warning_after_seconds: 1200, alert_after_seconds: 1290,
    rearm_after_seconds: 180, rearm_enabled: false, legacy_admission_enabled: true, expected_revision: 2,
  }))
})

test('authority read timeout leaves Mesh settings read-only despite a readable member and its confirmed Production topology', async () => {
  const env = setup('/meshes/mesh-prod/slots', true, true)
  const input = await screen.findByRole('spinbutton', { name: 'Session duration' })
  expect(input).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  expect(env.mutateMesh).not.toHaveBeenCalled()
})

test('a Mesh policy revision conflict preserves the unsaved draft', async () => {
  const env = setup()
  env.state.conflict = true
  const input = await editThreeHours()

  await userEvent.click(screen.getByRole('button', { name: 'Save' }))

  await waitFor(() => expect(env.loadControl.mock.calls.filter(([id]) => id === 'alpha')).toHaveLength(2))
  expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  expect(input).toHaveValue(3)
})

test('an exact newer policy read confirms a lost Mesh save receipt', async () => {
  const env = setup()
  env.state.loseReceipt = true
  await editThreeHours()

  await userEvent.click(screen.getByRole('button', { name: 'Save' }))

  await waitFor(() => expect(env.loadControl.mock.calls.filter(([id]) => id === 'alpha')).toHaveLength(2))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled())
  expect(screen.getByRole('spinbutton', { name: 'Session duration' })).toHaveValue(3)
  expect(env.mutateMesh).toHaveBeenCalledTimes(1)
})
