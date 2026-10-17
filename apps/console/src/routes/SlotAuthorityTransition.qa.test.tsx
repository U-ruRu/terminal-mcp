import { act,cleanup,fireEvent,render,screen,waitFor,within } from '@testing-library/react'
import { MemoryRouter,Route,Routes } from 'react-router-dom'
import { afterEach,expect,test,vi } from 'vitest'

import type { AccessMeshSlotReadModel } from '../api/models'
import { connectionAuthorityScope } from '../connections/authorityScope'
import type { FleetInstanceView } from '../fleet/types'
import { I18nProvider } from '../i18n/I18nProvider'
import { beginSlotOperation } from '../slots/operationJournal'
import { fleetInstance,nativeDefaults,nativeSlot,standaloneControl } from '../slots/testFixtures'
import { ServerSlots,type ServerSlotsProps } from './ServerSlots'

function Screen({ instances, loadSlots, mutate }: {
  instances: FleetInstanceView[]
  loadSlots: (id: string) => Promise<AccessMeshSlotReadModel[]>
  mutate: NonNullable<ServerSlotsProps['mutateAccessMesh']>
}) {
  return <I18nProvider><MemoryRouter initialEntries={['/slots']}><Routes>
    <Route path="/slots" element={<ServerSlots instances={instances} loadAccessMeshSlots={loadSlots}
      loadAccessMeshDefaults={loadDefaults} loadFleetControl={loadControl} mutateAccessMesh={mutate} />} />
  </Routes></MemoryRouter></I18nProvider>
}
const loadDefaults = async () => nativeDefaults()
const loadControl = async (id: string) => standaloneControl(id)

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

test('a late response from the previous authority cannot restore its rows after re-pair', async () => {
  const original = fleetInstance()
  let oldReply: (slots: AccessMeshSlotReadModel[]) => void = () => undefined
  const loadSlots = vi.fn<(id: string) => Promise<AccessMeshSlotReadModel[]>>()
    .mockImplementationOnce(() => new Promise((resolve) => { oldReply = resolve }))
    .mockResolvedValueOnce([nativeSlot('beta')])
  const mutate = vi.fn(async () => ({ ok: false, payload: {} }))
  const view = render(<Screen instances={[original]} loadSlots={loadSlots} mutate={mutate} />)
  const replacement: FleetInstanceView = { ...original, profile: { ...original.profile, origin: 'https://replacement.example', credentialRef: 'new-credential' } }
  view.rerender(<Screen instances={[replacement]} loadSlots={loadSlots} mutate={mutate} />)
  expect(await screen.findByRole('link', { name: 'Beta' })).toBeInTheDocument()
  await act(async () => { oldReply([nativeSlot()]) })
  expect(screen.getByRole('link', { name: 'Beta' })).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Alpha' })).not.toBeInTheDocument()
})

test('Recover submits the persisted native request and displays its receipt code', async () => {
  const instance = fleetInstance()
  const slot = nativeSlot('issued')
  slot.issuerNodeId = 'alpha'
  slot.revision = 1
  slot.codeGeneration = 1
  const request = { action: 'create' as const, mode: 'persistent' as const, idempotency_key: 'recover-issued-slot' }
  beginSlotOperation(connectionAuthorityScope(instance.profile), {
    backend: 'access-mesh', request, startedAt: '2026-10-12T05:00:00Z', phase: 'pending',
  })
  const loadSlots = vi.fn(async () => [slot])
  const mutate = vi.fn<NonNullable<ServerSlotsProps['mutateAccessMesh']>>(async () => ({
    ok: true, payload: { issuer_node_id: slot.issuerNodeId, slot_id: slot.slotId,
      logical_agent_id: slot.logicalAgentId, public_name: slot.publicName, revision: slot.revision,
      code_generation: 1, code_expires_at: null, access_code: '0099' },
  }))
  render(<Screen instances={[instance]} loadSlots={loadSlots} mutate={mutate} />)
  await screen.findByRole('link', { name: 'issued' })
  const recovery = document.querySelector('.slot-operation-recovery') as HTMLElement
  const recover = within(recovery).getByRole('button')
  await waitFor(() => expect(recover).toBeEnabled())

  fireEvent.click(recover)

  await waitFor(() => expect(mutate).toHaveBeenCalledWith('alpha', request))
  const createForm = screen.getByRole('button', { name: 'Create slot' }).closest('form')!
  const receipt = await within(createForm).findByRole('status')
  expect(within(receipt).getByText('0099', { selector: 'code' })).toBeInTheDocument()
})
