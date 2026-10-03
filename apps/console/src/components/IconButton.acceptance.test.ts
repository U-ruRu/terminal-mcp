import { describe, expect, it } from 'vitest'

import connectionsSource from '../routes/Connections.tsx?raw'
import overviewSource from '../routes/Overview.tsx?raw'
import slotsSource from '../routes/ServerSlots.tsx?raw'
import pairingSource from '../connections/PairingHandoffController.tsx?raw'
import diagnosticsSource from '../routes/ServerSection.tsx?raw'
import activitySource from '../routes/Activity.tsx?raw'
import shellSource from './AppShell.tsx?raw'

describe('IconButton acceptance regressions', () => {
  it('uses IconButtonRow for neighboring action controls on the mobile-sensitive surfaces', () => {
    expect(pairingSource).toContain('<IconButtonRow className="connection-actions">')
    expect(connectionsSource).toContain('<IconButtonRow className="connection-actions">')
    expect(slotsSource).toContain('<IconButtonRow className="server-actions">')
  })

  it('keeps destructive confirmation inside the shared UI instead of native confirm', () => {
    expect(connectionsSource).not.toContain('window.confirm(')
    expect(overviewSource).not.toContain('window.confirm(')
    expect(connectionsSource).toContain('<ConfirmationDialog')
    expect(overviewSource).toContain('<ConfirmationDialog')
    expect(slotsSource).toContain('<ConfirmationDialog')
  })

  it('retains IconButton migration across the required surfaces', () => {
    for (const source of [shellSource, activitySource, diagnosticsSource, slotsSource, overviewSource, connectionsSource, pairingSource]) {
      expect(source).toContain('IconButton')
    }
  })
})
