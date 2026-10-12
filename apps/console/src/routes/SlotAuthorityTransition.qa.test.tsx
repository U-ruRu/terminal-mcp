import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'

import type { AccessMeshSlotReadModel } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'
import { I18nProvider } from '../i18n/I18nProvider'
import { nativeDefaults, nativeSlot, fleetInstance, standaloneControl } from '../slots/testFixtures'
import { ServerSlots, type ServerSlotsProps } from './ServerSlots'
import { saveAccessCode } from '../access/codeVault'
import { nativeCodeScope } from '../slots/nativeCodeReceipt'

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

test('authority change under the same instance ID blocks old-slot actions until a new read', async () => {
  const original = fleetInstance()
  const loadSlots = vi.fn<(id: string) => Promise<AccessMeshSlotReadModel[]>>()
    .mockResolvedValueOnce([nativeSlot()])
    .mockImplementation(() => new Promise(() => undefined))
  const mutate = vi.fn(async () => ({ ok: false, code: 'isolated-not-committed', payload: { outcome: 'not_committed' } }))
  const view = render(<Screen instances={[original]} loadSlots={loadSlots} mutate={mutate} />)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled())
  const rePaired: FleetInstanceView = { ...original, profile: { ...original.profile,
    origin: 'https://replacement.example', credentialRef: 'cred-replacement',
    metadata: { ...original.profile.metadata, deviceId: 'replacement-device' },
  } }
  view.rerender(<Screen instances={[rePaired]} loadSlots={loadSlots} mutate={mutate} />)
  await waitFor(() => expect(loadSlots).toHaveBeenCalledTimes(2))
  // A last-observed row may remain visible, but it cannot authorize the new target.
  const pause = screen.queryByRole('button', { name: 'Pause' })
  if (pause) expect(pause).toBeDisabled()
  expect(mutate).not.toHaveBeenCalled()
})

test('two same-render create clicks send only one operation with a durable request key', async () => {
  const instance = fleetInstance()
  const loadSlots = vi.fn(async () => [nativeSlot()])
  const mutate = vi.fn<NonNullable<ServerSlotsProps['mutateAccessMesh']>>()
    .mockImplementation(() => new Promise(() => undefined))
  render(<Screen instances={[instance]} loadSlots={loadSlots} mutate={mutate} />)
  const create = await screen.findByRole('button', { name: 'Create slot' })
  await waitFor(() => expect(create).toBeEnabled())
  fireEvent.click(create); fireEvent.click(create)
  expect(mutate).toHaveBeenCalledTimes(1)
  const request = mutate.mock.calls[0][1]
  expect(request.action).toBe('create')
  expect(typeof request.idempotency_key).toBe('string')
  const stored = Object.keys(localStorage).filter((key) => key.includes('slot-operation')).map((key) => localStorage.getItem(key)).join('')
  expect(stored).toContain(request.idempotency_key)
  expect(stored).not.toContain('access_code')
})


test('reconnection with unchanged saved credentials still waits for a fresh read', async () => {
  const original = fleetInstance()
  const loadSlots = vi.fn<(id: string) => Promise<AccessMeshSlotReadModel[]>>()
    .mockResolvedValueOnce([nativeSlot()])
    .mockImplementation(() => new Promise(() => undefined))
  const mutate = vi.fn(async () => ({ ok: false, payload: {} }))
  const view = render(<Screen instances={[original]} loadSlots={loadSlots} mutate={mutate} />)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled())
  const disconnected: FleetInstanceView = { ...original, runtime: { ...original.runtime, authStatus: 'disconnected' } }
  view.rerender(<Screen instances={[disconnected]} loadSlots={loadSlots} mutate={mutate} />)
  view.rerender(<Screen instances={[original]} loadSlots={loadSlots} mutate={mutate} />)
  await waitFor(() => expect(loadSlots).toHaveBeenCalledTimes(2))
  const pause = screen.queryByRole('button', { name: 'Pause' })
  if (pause) expect(pause).toBeDisabled()
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

test('an uncertain create survives remount and recovers the original request without a second slot', async () => {
  const instance = fleetInstance()
  const slots: AccessMeshSlotReadModel[] = []
  const loadSlots = vi.fn(async () => structuredClone(slots))
  let firstResponse = true
  const mutate = vi.fn<NonNullable<ServerSlotsProps['mutateAccessMesh']>>(async () => {
    if (slots.length === 0) {
      const slot = nativeSlot('issued'); slot.issuerNodeId = 'alpha'; slot.revision = 1; slot.codeGeneration = 1
      slots.push(slot)
    }
    if (firstResponse) { firstResponse = false; throw new TypeError('synthetic network interruption') }
    const slot = slots[0]
    return { ok: true, payload: { issuer_node_id: slot.issuerNodeId, slot_id: slot.slotId,
      logical_agent_id: slot.logicalAgentId, public_name: slot.publicName, revision: slot.revision,
      code_generation: 1, code_expires_at: null, access_code: '0099' } }
  })
  let view = render(<Screen instances={[instance]} loadSlots={loadSlots} mutate={mutate} />)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Create slot' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Create slot' }))
  await waitFor(() => {
    expect(mutate).toHaveBeenCalledTimes(1)
    const recovery = document.querySelector('.slot-operation-recovery') as HTMLElement
    expect(within(recovery).getByRole('button')).toBeEnabled()
  })
  const originalRequest = structuredClone(mutate.mock.calls[0][1])
  view.unmount()
  view = render(<Screen instances={[instance]} loadSlots={loadSlots} mutate={mutate} />)
  expect(await screen.findByRole('link', { name: 'issued' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Create slot' })).toBeDisabled()
  const recovery = document.querySelector('.slot-operation-recovery') as HTMLElement
  fireEvent.click(within(recovery).getByRole('button'))
  await waitFor(() => expect(mutate).toHaveBeenCalledTimes(2))
  expect(mutate.mock.calls[1][1]).toEqual(originalRequest)
  expect(slots).toHaveLength(1)
  await waitFor(() => expect(document.querySelector('.slot-operation-recovery')).toBeNull())
  expect(screen.getAllByText('0099').length).toBeGreaterThan(0)
  view.unmount()
  render(<Screen instances={[instance]} loadSlots={loadSlots} mutate={mutate} />)
  expect((await screen.findAllByText('0099')).length).toBeGreaterThan(0)
  expect(mutate).toHaveBeenCalledTimes(2)
})

test('a displayed code is removed at its own expiry while the slot stays visible', async () => {
  const instance = fleetInstance(), slot = nativeSlot()
  let now = Date.now()
  vi.spyOn(Date, 'now').mockImplementation(() => now)
  saveAccessCode(slot.logicalAgentId, { code: '0077', generation: slot.codeGeneration!,
    expiresAt: new Date(now + 500).toISOString(), issuedRevision: slot.revision },
  localStorage, nativeCodeScope(slot.issuerNodeId, slot.slotId))
  const loadSlots = vi.fn(async () => [slot])
  const mutate = vi.fn(async () => ({ ok: false, payload: {} }))
  render(<Screen instances={[instance]} loadSlots={loadSlots} mutate={mutate} />)
  expect(await screen.findByText('0077')).toBeInTheDocument()
  now += 1000
  await waitFor(() => expect(screen.queryByText('0077')).not.toBeInTheDocument(), { timeout: 2500 })
  expect(screen.getByRole('link', { name: 'Alpha' })).toBeInTheDocument()
  expect(mutate).not.toHaveBeenCalled()
})

test.each(['pairedAt', 'scope'] as const)('same server and saved credentials invalidate successful reads when %s changes', async (field) => {
  const original = fleetInstance()
  let completeNewRead: (slots: AccessMeshSlotReadModel[]) => void = () => undefined
  const loadSlots = vi.fn<(id: string) => Promise<AccessMeshSlotReadModel[]>>()
    .mockResolvedValueOnce([nativeSlot()])
    .mockImplementationOnce(() => new Promise((resolve) => { completeNewRead = resolve }))
  const mutate = vi.fn(async () => ({ ok: false, payload: {} }))
  const view = render(<Screen instances={[original]} loadSlots={loadSlots} mutate={mutate} />)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled())
  const replacement: FleetInstanceView = { ...original, profile: { ...original.profile,
    metadata: { ...original.profile.metadata, ...(field === 'pairedAt'
      ? { pairedAt: original.profile.metadata.pairedAt + 1 }
      : { scope: original.profile.metadata.scope + ' terminal:audit' }) },
  } }
  view.rerender(<Screen instances={[replacement]} loadSlots={loadSlots} mutate={mutate} />)
  const oldPause = screen.queryByRole('button', { name: 'Pause' })
  if (oldPause) expect(oldPause).toBeDisabled()
  await waitFor(() => expect(loadSlots).toHaveBeenCalledTimes(2))
  expect(mutate).not.toHaveBeenCalled()
  await act(async () => { completeNewRead([nativeSlot('beta')]) })
  expect(await screen.findByRole('link', { name: 'Beta' })).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Alpha' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled()
})
