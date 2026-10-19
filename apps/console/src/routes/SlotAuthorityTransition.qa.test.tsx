import { act,cleanup,fireEvent,render,screen,waitFor,within } from '@testing-library/react'
import { MemoryRouter,Route,Routes } from 'react-router-dom'
import { afterEach,expect,test,vi } from 'vitest'

import type { AccessMeshSlotReadModel } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'
import { I18nProvider } from '../i18n/I18nProvider'
import { beginSlotOperation,loadSlotOperation,slotOperationScope } from '../slots/operationJournal'
import { fleetInstance,meshControl,nativeDefaults,nativeInheritance,nativeSlot,standaloneControl } from '../slots/testFixtures'
import { ServerSlots,type ServerSlotsProps } from './ServerSlots'

function Screen({ instances, loadSlots, mutate, control = loadControl, defaults = loadDefaults }: {
  instances: FleetInstanceView[]
  loadSlots: (id: string) => Promise<AccessMeshSlotReadModel[]>
  mutate: NonNullable<ServerSlotsProps['mutateAccessMesh']>
  control?: NonNullable<ServerSlotsProps['loadFleetControl']>
  defaults?: NonNullable<ServerSlotsProps['loadAccessMeshDefaults']>
}) {
  return <I18nProvider><MemoryRouter initialEntries={['/slots']}><Routes>
    <Route path="/slots" element={<ServerSlots instances={instances} loadAccessMeshSlots={loadSlots}
      loadAccessMeshDefaults={defaults} loadFleetControl={control} mutateAccessMesh={mutate} />} />
  </Routes></MemoryRouter></I18nProvider>
}
const loadDefaults = async () => nativeDefaults()
const loadControl = async (id: string) => standaloneControl(id)
const sharedControl = async (id: string) => meshControl(id)
const sharedDefaults = async () => ({ ...nativeDefaults(), meshPolicySupported: true, writeTarget: 'fleet_control' as const, inheritance: nativeInheritance('inherit', 'mesh') })

afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear(); vi.restoreAllMocks() })

test('changing a server authority disables writes until that authority is read', async () => {
  const original = fleetInstance()
  const loadSlots = vi.fn<(id: string) => Promise<AccessMeshSlotReadModel[]>>()
    .mockResolvedValueOnce([nativeSlot()])
    .mockImplementation(() => new Promise(() => undefined))
  const mutate = vi.fn(async () => ({ ok: false, code: 'not_committed', payload: { outcome: 'not_committed' } }))
  const view = render(<Screen instances={[original]} loadSlots={loadSlots} mutate={mutate} />)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled())
  const replacement: FleetInstanceView = { ...original, profile: { ...original.profile,
    origin: 'https://replacement.example', credentialRef: 'cred-replacement',
    metadata: { ...original.profile.metadata, deviceId: 'replacement-device' },
  } }

  view.rerender(<Screen instances={[replacement]} loadSlots={loadSlots} mutate={mutate} />)

  await waitFor(() => expect(loadSlots).toHaveBeenCalledTimes(2))
  expect(screen.getByRole('button', { name: 'Create slot' })).toBeDisabled()
})

test('repeated create clicks in one render send a single keyed operation', async () => {
  const loadSlots = vi.fn(async () => [nativeSlot()])
  const mutate = vi.fn<NonNullable<ServerSlotsProps['mutateAccessMesh']>>()
    .mockImplementation(() => new Promise(() => undefined))
  render(<Screen instances={[fleetInstance()]} loadSlots={loadSlots} mutate={mutate} />)
  const create = await screen.findByRole('button', { name: 'Create slot' })
  await waitFor(() => expect(create).toBeEnabled())

  fireEvent.click(create)
  fireEvent.click(create)

  expect(mutate).toHaveBeenCalledTimes(1)
  expect(mutate.mock.calls[0][1]).toMatchObject({ action: 'create', idempotency_key: expect.any(String) })
})

test.each(['pairing', 'registry'])('a late response cannot restore rows after a %s change', async (change) => {
  const original = fleetInstance()
  let oldReply: (slots: AccessMeshSlotReadModel[]) => void = () => undefined
  const loadSlots = vi.fn<(id: string) => Promise<AccessMeshSlotReadModel[]>>()
    .mockImplementationOnce(() => new Promise((resolve) => { oldReply = resolve }))
    .mockResolvedValueOnce([nativeSlot('beta')])
  const mutate = vi.fn(async () => ({ ok: false, payload: {} }))
  const view = render(<Screen instances={[original]} loadSlots={loadSlots} mutate={mutate} />)
  await waitFor(() => expect(loadSlots).toHaveBeenCalledTimes(1))
  const replacement: FleetInstanceView = change === 'pairing' ? { ...original, profile: { ...original.profile, origin: 'https://replacement.example', credentialRef: 'new-credential' } } : original
  view.rerender(<Screen instances={[replacement]} loadSlots={loadSlots} mutate={mutate} control={change === 'registry' ? sharedControl : loadControl} defaults={change === 'registry' ? sharedDefaults : loadDefaults} />)
  expect(await screen.findByRole('link', { name: 'Beta' })).toBeInTheDocument()
  await act(async () => { oldReply([nativeSlot()]) })
  expect(screen.getByRole('link', { name: 'Beta' })).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Alpha' })).not.toBeInTheDocument()
})

test.each(['standalone', 'mesh-member'])('Recover through the original %s pairing submits its persisted request and receipt', async (kind) => {
  const shared = kind === 'mesh-member'
  const instance = fleetInstance(shared ? 'beta' : 'alpha')
  const slot = nativeSlot('issued')
  slot.issuerNodeId = 'alpha'
  slot.revision = 1
  slot.codeGeneration = 1
  const request = { action: 'create' as const, mode: 'persistent' as const, idempotency_key: 'recover-issued-slot' }
  beginSlotOperation(slotOperationScope(JSON.stringify([shared ? 'mesh:mesh-prod' : 'server:alpha', 'alpha']), instance.profile), {
    backend: 'access-mesh', request, startedAt: '2026-10-12T05:00:00Z', phase: 'pending',
  })
  const loadSlots = vi.fn(async () => [slot])
  const mutate = vi.fn<NonNullable<ServerSlotsProps['mutateAccessMesh']>>(async () => ({
    ok: true, payload: { issuer_node_id: slot.issuerNodeId, slot_id: slot.slotId,
      public_name: slot.publicName, revision: slot.revision,
      code_generation: 1, code_expires_at: null, access_code: '0099' },
  }))
  render(<Screen instances={shared ? [fleetInstance(), instance] : [instance]} loadSlots={loadSlots} mutate={mutate} control={shared ? sharedControl : loadControl} defaults={shared ? sharedDefaults : loadDefaults} />)
  await screen.findByRole('link', { name: 'issued' })
  const recovery = document.querySelector('.slot-operation-recovery') as HTMLElement
  const recover = within(recovery).getByRole('button')
  await waitFor(() => expect(recover).toBeEnabled())

  fireEvent.click(recover)

  await waitFor(() => expect(mutate).toHaveBeenCalledWith(instance.profile.instanceId, request))
  const createForm = screen.getByRole('button', { name: 'Create slot' }).closest('form')!
  const receipt = await within(createForm).findByRole('status')
  expect(within(receipt).getByText('0099', { selector: 'code' })).toBeInTheDocument()
})

test('a journal from the prior registry is not offered for recovery in the new Mesh', async () => {
  const instance = fleetInstance()
  const scope = slotOperationScope(JSON.stringify(['server:alpha', 'alpha']), instance.profile)
  beginSlotOperation(scope, { backend: 'access-mesh', request: { action: 'create', mode: 'persistent', idempotency_key: 'old-registry-create' }, startedAt: '2026-10-12T05:00:00Z', phase: 'pending' })
  const mutate = vi.fn(async () => ({ ok: false, payload: {} }))

  render(<Screen instances={[instance]} loadSlots={async () => [nativeSlot()]} mutate={mutate} control={sharedControl} defaults={sharedDefaults} />)

  await waitFor(() => expect(screen.getByRole('button', { name: 'Create slot' })).toBeEnabled())
  expect(screen.queryByRole('button', { name: 'Recover' })).not.toBeInTheDocument()
  expect(loadSlotOperation(scope)?.request.idempotency_key).toBe('old-registry-create')
  expect(mutate).not.toHaveBeenCalled()
})

test.each([
  { code: 'identity_metadata_missing', payload: {} },
  { code: 'registry_blocked', payload: { outcome: 'not_committed' } },
])('a definite $code rejection clears its pending operation', async (result) => {
  const instance = fleetInstance()
  const mutate = vi.fn(async () => ({ ok: false, ...result }))
  render(<Screen instances={[instance]} loadSlots={async () => [nativeSlot()]} mutate={mutate} />)
  const create = await screen.findByRole('button', { name: 'Create slot' })
  await waitFor(() => expect(create).toBeEnabled())

  fireEvent.click(create)

  await waitFor(() => expect(mutate).toHaveBeenCalledTimes(1))
  await waitFor(() => expect(create).toBeEnabled())
  expect(loadSlotOperation(slotOperationScope(JSON.stringify(['server:alpha', 'alpha']), instance.profile))).toBeNull()
  expect(screen.queryByRole('button', { name: 'Recover' })).not.toBeInTheDocument()
})

test('a delete receipt from the prior registry cannot confirm against the new registry absence', async () => {
  const instance = fleetInstance()
  const scope = slotOperationScope(JSON.stringify(['server:alpha', 'alpha']), instance.profile)
  let finishDelete: (result: { ok: boolean; payload: { revision: number } }) => void = () => undefined
  const mutate = vi.fn<NonNullable<ServerSlotsProps['mutateAccessMesh']>>(() => new Promise((resolve) => { finishDelete = resolve }))
  const loadSlots = vi.fn(async () => [nativeSlot()])
  const view = render(<Screen instances={[instance]} loadSlots={loadSlots} mutate={mutate} />)
  await screen.findByRole('link', { name: 'Alpha' })
  fireEvent.click(screen.getByLabelText('More actions'))
  fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }))
  await waitFor(() => expect(mutate).toHaveBeenCalledTimes(1))
  loadSlots.mockResolvedValue([])
  view.rerender(<Screen instances={[instance]} loadSlots={loadSlots} mutate={mutate} control={sharedControl} defaults={sharedDefaults} />)
  await waitFor(() => expect(loadSlots).toHaveBeenCalledTimes(2))

  await act(async () => { finishDelete({ ok: true, payload: { revision: 8 } }) })

  expect(loadSlotOperation(scope)?.phase).toBe('accepted')
  expect(mutate).toHaveBeenCalledTimes(1)
  expect(screen.queryByText('Confirmed by server')).not.toBeInTheDocument()
})
