import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'

import type { PersistentMutationResult } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'
import { I18nProvider } from '../i18n/I18nProvider'
import { ManagedWorkSessions } from './ManagedWorkSessions'

const STATUS = {
  ok: true,
  logical_agent_id: 'la_one',
  public_name: 'Stable-Agent',
  authority_node_id: 'secondary',
  authority_epoch: 3,
  policy: {
    default_duration_seconds: 1380,
    warning_before_expiry_seconds: 180,
    draining_before_expiry_seconds: 90,
    rearm_after_seconds: 180,
    revision: 4,
  },
  window: {
    work_window_id: 'ww_one',
    state: 'active',
    lifecycle: 'open',
    opened_at: '2026-10-07T04:00:00Z',
    elapsed_seconds: 120,
    remaining_seconds: 1260,
    initial_duration_seconds: 1380,
    effective_duration_seconds: 1380,
    hard_expires_at: '2026-10-07T04:23:00Z',
    warning_before_expiry_seconds: 180,
    draining_before_expiry_seconds: 90,
    rearm_after_seconds: 180,
    rearm_at: null,
    window_revision: 7,
  },
  session: {
    work_session_id: 'ws_one',
    session_epoch: 2,
    role: 'executor',
    contract_version: 1,
    state: 'active',
    started_at: '2026-10-07T04:01:00Z',
    ended_at: null,
    end_reason: null,
  },
}

function instance(): FleetInstanceView {
  return {
    profile: {
      instanceId: 'secondary', origin: 'https://secondary.example', displayName: 'Secondary',
      credentialRef: 'cred', metadata: { deviceId: 'd', clientId: 'c', deviceLabel: 'Console', scope: 'terminal:read', pairedAt: 1 },
      createdAt: 1, updatedAt: 1,
    },
    runtime: {
      instanceId: 'secondary', status: 'live', authStatus: 'connected', reconnectAttempt: 0,
      realtime: {
        status: 'live', cursor: 1, highWaterSeq: 1, socketConnected: true, reconnectAttempt: 0,
        freshness: 'fresh', catchingUpScopes: [],
        snapshot: {
          highWaterSeq: 1, replayFromSeq: 1, duplicateEventsPossible: false,
          instance: { instanceId: 'secondary', displayName: 'Secondary', version: 'test', hostname: 'secondary', startedAt: '', serverNow: '' },
          agents: [], tasks: [], contexts: [], communications: [],
          persistent: {
            enabled: true, available: true,
            policy: { durationSeconds: 1380, warningAfterSeconds: 1200, alertAfterSeconds: 1320, rearmAfterSeconds: 180, legacyAdmissionEnabled: false, manualRearm: false, admissionMode: 'persistent', policyControlSupported: true },
            slots: [{
              logicalAgentId: 'la_one', displayName: 'Stable-Agent', state: 'active', authorityNodeId: 'secondary', authorityEpoch: 3,
              slotRevision: 6, selector: 'ABCD', selectorGeneration: 1, authGeneration: 1, createdAt: '', updatedAt: '', serverNow: '',
              workSession: { workSessionId: 'ws_one', sessionEpoch: 2, authorityNodeId: 'secondary', authorityEpoch: 3, startedAt: '', hardExpiresAt: '', state: 'active' },
              claims: [], audit: [], attachments: [],
            }],
          },
        },
      },
    },
  } as unknown as FleetInstanceView
}

function result(payload: Record<string, unknown>): PersistentMutationResult {
  return { ok: true, payload }
}

test('managed Activity controls use canonical status and CAS mutations', async () => {
  const mutate = vi.fn(async (_instanceId: string, path: string, body: Record<string, unknown>) => {
    void body
    if (path === '/actions/persistent/managed/status') return result(STATUS)
    return result({ ok: true })
  })
  render(<I18nProvider><ManagedWorkSessions instance={instance()} mutatePersistent={mutate} /></I18nProvider>)

  expect(await screen.findByText('Stable-Agent')).toBeInTheDocument()
  expect(screen.getByText('executor · v1')).toBeInTheDocument()
  expect(screen.getByText('Secondary')).toBeInTheDocument()

  await userEvent.click(screen.getByRole('button', { name: '+10 min' }))
  await waitFor(() => expect(mutate).toHaveBeenCalledWith('secondary', '/actions/persistent/managed/window', {
    logical_agent_id: 'la_one', expected_revision: 7, delta_seconds: 600,
  }))

  await userEvent.clear(screen.getByLabelText('Custom delta (seconds)'))
  await userEvent.type(screen.getByLabelText('Custom delta (seconds)'), '-300')
  await userEvent.click(screen.getByRole('button', { name: 'Apply delta' }))
  await waitFor(() => expect(mutate).toHaveBeenCalledWith('secondary', '/actions/persistent/managed/window', {
    logical_agent_id: 'la_one', expected_revision: 7, delta_seconds: -300,
  }))

  await userEvent.click(screen.getByRole('button', { name: 'End work session' }))
  await waitFor(() => expect(mutate).toHaveBeenCalledWith(
    'secondary', '/actions/persistent/managed/sessions/end', { logical_agent_id: 'la_one' },
  ))
})
