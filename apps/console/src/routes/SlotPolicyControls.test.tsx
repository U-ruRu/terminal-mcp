import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'

import { ConsoleHttpError } from '../api/client'
import type { ManagedSlotStatusReadModel, PersistentMutationResult } from '../api/models'
import { I18nProvider } from '../i18n/I18nProvider'
import { SlotPolicyControls, type ManagedSlotStatusLoader } from './SlotPolicyControls'

type Mutator = (instanceId: string, path: string, body: Record<string, unknown>) => Promise<PersistentMutationResult>

function status(revision = 1, duration = 1380): ManagedSlotStatusReadModel {
  return {
    logicalAgentId: 'la_alpha', publicName: 'Alpha', authorityNodeId: 'alpha', authorityEpoch: 1,
    policy: { defaultDurationSeconds: duration, warningBeforeExpirySeconds: 180, drainingBeforeExpirySeconds: 90, rearmAfterSeconds: 180, revision },
    window: {
      workWindowId: 'ww_alpha', state: 'active', lifecycle: 'open', openedAt: '2026-10-12T10:00:00Z', elapsedSeconds: 120,
      remainingSeconds: 1260, initialDurationSeconds: 1380, effectiveDurationSeconds: 1380, hardExpiresAt: '2026-10-12T10:23:00Z',
      warningBeforeExpirySeconds: 180, drainingBeforeExpirySeconds: 90, rearmAfterSeconds: 180, rearmAt: null, windowRevision: 1,
    },
    session: { workSessionId: 'ws_alpha', sessionEpoch: 1, role: 'executor', contractVersion: '1', state: 'active', startedAt: '2026-10-12T10:00:00Z', endedAt: null, endReason: null },
  }
}

function renderPolicy(loadStatus: ManagedSlotStatusLoader, mutate?: Mutator) {
  return render(<I18nProvider><SlotPolicyControls instanceId="alpha" logicalAgentId="la_alpha" connected loadStatus={loadStatus} mutate={mutate} /></I18nProvider>)
}

afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks() })

test('three hours saves 10800 seconds for this slot and rereads policy without changing the open window', async () => {
  const user = userEvent.setup()
  const load = vi.fn().mockResolvedValueOnce(status()).mockResolvedValue(status(2, 10800))
  const mutate = vi.fn<Mutator>(async (): Promise<PersistentMutationResult> => ({ ok: true, payload: {} }))
  renderPolicy(load, mutate)
  const duration = await screen.findByRole('spinbutton', { name: 'Slot session duration' })
  await user.clear(duration); await user.type(duration, '3')
  await user.selectOptions(screen.getByRole('combobox', { name: 'Slot session duration · Unit' }), 'hours')
  await user.click(screen.getByRole('button', { name: 'Save slot policy' }))
  await screen.findByText('Server values confirmed.')
  expect(mutate).toHaveBeenCalledExactlyOnceWith('alpha', '/actions/persistent/managed/policy', {
    logical_agent_id: 'la_alpha', expected_revision: 1, default_duration_seconds: 10800,
    warning_before_expiry_seconds: 180, draining_before_expiry_seconds: 90, rearm_after_seconds: 180,
  })
  expect(load).toHaveBeenCalledTimes(2)
  expect(duration).toHaveValue(3)
  expect(screen.getByRole('combobox', { name: 'Slot session duration · Unit' })).toHaveValue('hours')
  expect(screen.getByRole('spinbutton', { name: 'Total window duration' })).toHaveValue(23)
  expect(screen.getByText('2026-10-12T10:23:00Z')).toBeInTheDocument()
})

test('managed waiting and expiry thresholds accept zero without applying legacy after-start constraints', async () => {
  const user = userEvent.setup()
  const initial = status(); initial.policy.warningBeforeExpirySeconds = 0; initial.policy.drainingBeforeExpirySeconds = 0; initial.policy.rearmAfterSeconds = 0
  const load = vi.fn(async () => initial)
  const mutate = vi.fn<Mutator>(async (): Promise<PersistentMutationResult> => ({ ok: true, payload: {} }))
  renderPolicy(load, mutate)
  await screen.findByRole('spinbutton', { name: 'Reopen wait' })
  await user.click(screen.getByRole('button', { name: 'Save slot policy' }))
  await screen.findByText('Server values confirmed.')
  expect(mutate.mock.calls[0][2]).toMatchObject({ warning_before_expiry_seconds: 0, draining_before_expiry_seconds: 0, rearm_after_seconds: 0 })
})

test('a revision conflict preserves the draft and requires explicit cancellation before another save', async () => {
  const user = userEvent.setup()
  const load = vi.fn().mockResolvedValueOnce(status()).mockResolvedValue(status(2, 3600))
  const mutate = vi.fn<Mutator>(async (): Promise<PersistentMutationResult> => ({ ok: false, code: 'slot_policy_revision_conflict', payload: {} }))
  renderPolicy(load, mutate)
  const duration = await screen.findByRole('spinbutton', { name: 'Slot session duration' })
  await user.clear(duration); await user.type(duration, '5')
  await user.click(screen.getByRole('button', { name: 'Save slot policy' }))
  await waitFor(() => expect(load).toHaveBeenCalledTimes(2))
  expect(duration).toHaveValue(5)
  expect(screen.getByRole('button', { name: 'Save slot policy' })).toBeDisabled()
  expect(mutate).toHaveBeenCalledTimes(1)
  await user.click(screen.getByRole('button', { name: 'Cancel slot policy edits' }))
  expect(duration).toHaveValue(1)
  expect(screen.getByRole('combobox', { name: 'Slot session duration · Unit' })).toHaveValue('hours')
  expect(screen.getByRole('button', { name: 'Save slot policy' })).toBeEnabled()
})

test('an uncertain response cannot be repeated while the authoritative reread is failing', async () => {
  const user = userEvent.setup()
  const load = vi.fn().mockResolvedValueOnce(status()).mockRejectedValue(new Error('network_failed'))
  const mutate = vi.fn<Mutator>(async (): Promise<PersistentMutationResult> => { throw new Error('private response body must not appear') })
  renderPolicy(load, mutate)
  await screen.findByRole('spinbutton', { name: 'Slot session duration' })
  await user.click(screen.getByRole('button', { name: 'Save slot policy' }))
  await screen.findByText(/The network response is uncertain/)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save slot policy' })).toBeDisabled())
  expect(screen.queryByText(/private response body/)).not.toBeInTheDocument()
  expect(mutate).toHaveBeenCalledTimes(1)
  load.mockResolvedValue(status())
  await user.click(screen.getByRole('button', { name: 'Refresh slot settings' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save slot policy' })).toBeEnabled())
  expect(mutate).toHaveBeenCalledTimes(1)
})

test('two clicks batched before React rerenders produce one policy request', async () => {
  let resolve!: (value: PersistentMutationResult) => void
  const pending = new Promise<PersistentMutationResult>((done) => { resolve = done })
  const load = vi.fn(async () => status())
  const mutate = vi.fn(() => pending)
  renderPolicy(load, mutate)
  const save = await screen.findByRole('button', { name: 'Save slot policy' })
  act(() => { fireEvent.click(save); fireEvent.click(save) })
  expect(mutate).toHaveBeenCalledTimes(1)
  await act(async () => { resolve({ ok: true, payload: {} }); await pending })
  await screen.findByText('Server values confirmed.')
})

test('404 and permission failures are distinct from empty policy data', async () => {
  const missing = vi.fn(async () => { throw new ConsoleHttpError(404, 'not_found') })
  const view = renderPolicy(missing)
  await screen.findByText('This server does not support per-slot session settings.')
  expect(screen.queryByRole('spinbutton')).not.toBeInTheDocument()
  view.unmount()
  const denied = vi.fn(async () => { throw new ConsoleHttpError(403, 'insufficient_scope') })
  renderPolicy(denied)
  await screen.findByText('This connection does not have permission to access slot settings.')
  expect(screen.queryByRole('spinbutton')).not.toBeInTheDocument()
})

test('window changes have a separate explicit confirmation and Cancel makes no request', async () => {
  const user = userEvent.setup()
  const load = vi.fn(async () => status())
  const mutate = vi.fn<Mutator>(async (): Promise<PersistentMutationResult> => ({ ok: true, payload: {} }))
  renderPolicy(load, mutate)
  const duration = await screen.findByRole('spinbutton', { name: 'Total window duration' })
  await user.clear(duration); await user.type(duration, '3')
  await user.selectOptions(screen.getByRole('combobox', { name: 'Total window duration · Unit' }), 'hours')
  await user.click(screen.getByRole('button', { name: 'Save current window' }))
  expect(mutate).not.toHaveBeenCalled()
  await user.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(mutate).not.toHaveBeenCalled()
  await user.click(screen.getByRole('button', { name: 'Save current window' }))
  await user.click(screen.getByRole('button', { name: 'Apply window change' }))
  await screen.findByText('Server values confirmed.')
  expect(mutate).toHaveBeenCalledExactlyOnceWith('alpha', '/actions/persistent/managed/window', {
    logical_agent_id: 'la_alpha', expected_revision: 1, duration_seconds: 10800,
    warning_before_expiry_seconds: 180, draining_before_expiry_seconds: 90,
  })
})

test('closed windows remain readable and cannot be reopened by a window edit', async () => {
  const expired = status(); expired.window!.lifecycle = 'expired'; expired.window!.state = 'expired'
  renderPolicy(vi.fn(async () => expired))
  await screen.findByText('This work window has ended. Its deadline cannot be edited here.')
  expect(screen.queryByRole('button', { name: 'Save current window' })).not.toBeInTheDocument()
})

test('leaving Slot Details discards draft-only changes without a policy request', async () => {
  const user = userEvent.setup()
  const load = vi.fn(async () => status())
  const mutate = vi.fn<Mutator>(async (): Promise<PersistentMutationResult> => ({ ok: true, payload: {} }))
  const view = renderPolicy(load, mutate)
  const input = await screen.findByRole('spinbutton', { name: 'Slot session duration' })
  await user.clear(input); await user.type(input, '9')
  view.unmount()
  renderPolicy(load, mutate)
  expect(await screen.findByRole('spinbutton', { name: 'Slot session duration' })).toHaveValue(23)
  expect(mutate).not.toHaveBeenCalled()
})
