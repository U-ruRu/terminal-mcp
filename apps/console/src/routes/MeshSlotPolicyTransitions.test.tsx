import { cleanup,fireEvent,render,screen,waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter,Route,Routes } from 'react-router-dom'
import { afterEach,expect,test,vi } from 'vitest'
import { ConsoleHttpError } from '../api/client'
import type { AccessMeshMutationRequest,ManagedAccessPolicyReadModel,ManagedFleetControlReadModel,AccessMutationResult } from '../api/models'
import { I18nProvider } from '../i18n/I18nProvider'
import { fleetInstance,meshControl,nativeDefaults,nativeInheritance,nativeSlot } from '../slots/testFixtures'
import type { MeshAccessPolicyRequest } from './MeshAccessPolicyControls'
import * as PolicyControls from './MeshAccessPolicyControls'
import { ServerSlots } from './ServerSlots'

function setup(route = '/meshes/mesh-prod/slots', capability = true, failAuthority = false, memberOnly = false, failAllControls = false) {
  const state = {
    policy: { durationSeconds: 1380, warningAfterSeconds: 1200, alertAfterSeconds: 1290, rearmAfterSeconds: 180,
      rearmEnabled: true, legacyAdmissionEnabled: false,
      revision: 2, updatedAt: '2026-10-12T05:00:00Z' } as ManagedAccessPolicyReadModel,
    slots: [nativeSlot()], defaults: { ...nativeDefaults(), meshPolicySupported: capability, inheritanceSupported: capability,
      inheritance: nativeInheritance('inherit', 'mesh'), writeTarget: 'fleet_control' as const },
    failAuthority, failAllControls, conflicts: 0, loseReceipt: false as false | 'network' | 'unconfirmed', meshId: 'mesh-prod', replyMeshId: '',
  }
  const control = (id: string): ManagedFleetControlReadModel => {
    const value = meshControl(id)
    return { ...value, policy: structuredClone(state.policy), mesh: { ...value.mesh!, meshId: state.meshId },
      meshes: value.meshes.map((mesh) => ({ ...mesh, meshId: state.meshId })),
      nodes: value.nodes.map((node) => ({ ...node, meshId: state.meshId })) }
  }
  const loadControl = vi.fn(async (id: string) => {
    if (state.failAllControls || id === 'alpha' && state.failAuthority) throw new ConsoleHttpError(0, 'request_timeout')
    return control(id)
  })
  const loadSlots = vi.fn(async () => structuredClone(state.slots))
  const loadDefaults = vi.fn(async () => structuredClone(state.defaults))
  const mutateMesh = vi.fn(async (_id: string, _path: string, body: Record<string, unknown>) => {
    const request = body as MeshAccessPolicyRequest
    if (state.conflicts > 0) {
      state.conflicts--
      state.policy = { ...state.policy, revision: state.policy.revision + 1, durationSeconds: 7200 }
      state.defaults.inheritance.parent!.revision = state.policy.revision
      throw new ConsoleHttpError(409, 'access_policy_revision_conflict')
    }
    state.policy = { durationSeconds: request.duration_seconds, warningAfterSeconds: request.warning_after_seconds,
      alertAfterSeconds: request.alert_after_seconds, rearmAfterSeconds: request.rearm_after_seconds,
      rearmEnabled: request.rearm_enabled, legacyAdmissionEnabled: request.legacy_admission_enabled,
      revision: request.expected_revision + 1, updatedAt: state.policy.updatedAt }
    state.defaults.inheritance.parent!.revision = state.policy.revision
    if (state.replyMeshId) {
      state.meshId = state.replyMeshId
      state.defaults.inheritance.parent!.id = state.meshId
    }
    if (state.loseReceipt === 'network') throw new ConsoleHttpError(0, 'request_timeout')
    if (state.loseReceipt === 'unconfirmed') return { ok: false, code: 'control_mutation_unconfirmed' }
    return { ok: true, control: control('alpha') }
  })
  const mutateNative = vi.fn<(id: string, request: AccessMeshMutationRequest) => Promise<AccessMutationResult>>(async () => ({ ok: true, payload: {} }))
  const props = { instances: memberOnly ? [fleetInstance('beta')] : [fleetInstance(), fleetInstance('beta')], loadFleetControl: loadControl, loadAccessMeshSlots: loadSlots,
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

test.each([
  ['/meshes/mesh-prod/slots', false, 'alpha'],
  ['/servers/beta/slots', false, 'alpha'],
  ['/servers/beta/slots', true, 'beta'],
])('saving Mesh policy through %s (member-only pairing: %s) keeps its shared owner', async (route, memberOnly, ingress) => {
  const env = setup(route, true, false, memberOnly)
  await editThreeHours()
  await userEvent.click(screen.getByRole('checkbox', { name: 'Legacy admission' }))
  await userEvent.click(screen.getByRole('checkbox', { name: 'Rearm' }))

  await userEvent.click(screen.getByRole('button', { name: 'Save' }))

  await waitFor(() => expect(env.mutateMesh).toHaveBeenCalledWith(ingress, '/actions/fleet/control/policy', {
    duration_seconds: 10800, warning_after_seconds: 1200, alert_after_seconds: 1290,
    rearm_after_seconds: 180, rearm_enabled: false, legacy_admission_enabled: true, expected_revision: 2,
  }))
  expect(env.mutateNative).not.toHaveBeenCalled()
})

test('a member saves with the current authority parent revision despite its older local policy', async () => {
  const env = setup('/meshes/mesh-prod/slots', true, true)
  env.state.defaults.inheritance.parent!.revision = 3
  await editThreeHours()

  await userEvent.click(screen.getByRole('button', { name: 'Save' }))

  await waitFor(() => expect(env.mutateMesh).toHaveBeenCalledWith('beta', '/actions/fleet/control/policy', expect.objectContaining({
    expected_revision: 3, duration_seconds: 10800, warning_after_seconds: 1200, alert_after_seconds: 1290,
  })))
})

test.each([1, 2])('a Mesh policy Save retries only one definite CAS race (%s conflicts)', async (conflicts) => {
  const env = setup()
  env.state.conflicts = conflicts
  const input = await editThreeHours()

  await userEvent.click(screen.getByRole('button', { name: 'Save' }))

  await waitFor(() => expect(env.mutateMesh).toHaveBeenCalledTimes(2))
  expect(env.mutateMesh.mock.calls[1][2]).toEqual({
    duration_seconds: 10800, warning_after_seconds: 1200, alert_after_seconds: 1290,
    rearm_after_seconds: 180, rearm_enabled: true, legacy_admission_enabled: false, expected_revision: 3,
  })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled())
  expect(input).toHaveValue(3)
})

test.each([
  { mode: 'network', sameMesh: true },
  { mode: 'unconfirmed', sameMesh: true },
  { mode: 'unconfirmed', sameMesh: false },
  { mode: 'no-op', sameMesh: true },
] as const)('an exact $mode policy read confirms only its original Mesh (same: $sameMesh)', async ({ mode, sameMesh }) => {
  const outcomes = vi.fn()
  const Component = PolicyControls.MeshAccessPolicyControls
  vi.spyOn(PolicyControls, 'MeshAccessPolicyControls').mockImplementation((props) => <Component {...props} save={async (request) => {
    const result = await props.save(request)
    outcomes(result)
    return result
  }} />)
  const env = setup()
  if (mode === 'no-op') env.mutateMesh.mockResolvedValueOnce({ ok: true, control: meshControl('alpha') })
  else env.state.loseReceipt = mode
  if (!sameMesh) env.state.replyMeshId = 'mesh-other'
  if (mode === 'no-op') await screen.findByRole('spinbutton', { name: 'Session duration' })
  else await editThreeHours()

  await userEvent.click(screen.getByRole('button', { name: 'Save' }))

  await waitFor(() => expect(env.loadControl.mock.calls.filter(([id]) => id === 'alpha')).toHaveLength(3))
  await waitFor(() => expect(outcomes).toHaveBeenCalledWith(expect.objectContaining({ confirmed: sameMesh })))
  if (sameMesh) {
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled())
    expect(screen.getByRole('spinbutton', { name: 'Session duration' })).toHaveValue(mode === 'no-op' ? 23 : 3)
  }
  expect(env.mutateMesh).toHaveBeenCalledTimes(1)
})

test('an unresolved Mesh route can retry its initial direct control failure', async () => {
  const env = setup('/meshes/mesh-prod/slots', true, false, false, true)
  await waitFor(() => expect(env.loadControl).toHaveBeenCalledTimes(2))
  expect(screen.queryByRole('spinbutton', { name: 'Session duration' })).not.toBeInTheDocument()
  env.state.failAllControls = false

  await userEvent.click(screen.getByRole('button', { name: 'Refresh slot settings' }))

  expect(await screen.findByRole('spinbutton', { name: 'Session duration' })).toBeEnabled()
  expect(env.loadControl).toHaveBeenCalledTimes(4)
  expect(env.mutateMesh).not.toHaveBeenCalled()
})
