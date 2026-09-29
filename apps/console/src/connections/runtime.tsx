import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'

import { PairingTransport } from '../auth/transport'
import { BrowserConnectionRegistry } from './registry'
import type { ConnectionProfile, ProfileRestoreResult } from './types'

export type RuntimeProfileState =
  | { status: 'restoring' }
  | { status: 'connected'; accessToken: string; accessExpiresAt: number }
  | { status: 'revoked' | 'expired' }
  | { status: 'error'; retryable: boolean; message: string }

type ConnectionRuntimeValue = {
  profiles: ConnectionProfile[]
  states: Record<string, RuntimeProfileState>
  error: string | null
  pair: (pairingLink: string, displayName?: string) => Promise<void>
  retry: (instanceId: string) => Promise<void>
  disconnect: (instanceId: string) => void
}

const ConnectionRuntimeContext = createContext<ConnectionRuntimeValue | null>(null)

function stateFromRestore(result: ProfileRestoreResult): RuntimeProfileState | null {
  switch (result.status) {
    case 'connected':
      return {
        status: 'connected',
        accessToken: result.accessToken,
        accessExpiresAt: result.accessExpiresAt,
      }
    case 'revoked':
    case 'expired':
      return { status: result.status }
    case 'error':
      return {
        status: 'error',
        retryable: result.retryable,
        message: result.message,
      }
    case 'missing':
      return null
  }
}

export function ConnectionRuntimeProvider({
  children,
  registry: registryProp,
  transport: transportProp,
  deviceLabel = 'Terminal MCP Console',
  onProfilesChanged,
}: {
  children: ReactNode
  registry?: BrowserConnectionRegistry
  transport?: PairingTransport
  deviceLabel?: string
  onProfilesChanged?: () => void
}) {
  const [registry] = useState(() => registryProp ?? new BrowserConnectionRegistry())
  const [transport] = useState(() => transportProp ?? new PairingTransport())
  const [profiles, setProfiles] = useState<ConnectionProfile[]>(() => registry.list())
  const [states, setStates] = useState<Record<string, RuntimeProfileState>>({})
  const [error, setError] = useState<string | null>(null)
  const restoreStarted = useRef(false)

  const syncProfiles = useCallback(() => {
    const next = registry.list()
    setProfiles(next)
    return next
  }, [registry])

  const restoreOne = useCallback(
    async (instanceId: string) => {
      setStates((current) => ({
        ...current,
        [instanceId]: { status: 'restoring' },
      }))
      const result = await registry.restore(instanceId, transport)
      const nextState = stateFromRestore(result)
      setStates((current) => {
        const next = { ...current }
        if (nextState) next[instanceId] = nextState
        else delete next[instanceId]
        return next
      })
    },
    [registry, transport],
  )

  useEffect(() => {
    if (restoreStarted.current) return
    restoreStarted.current = true
    const current = registry.list()
    void Promise.resolve().then(() =>
      Promise.all(current.map((profile) => restoreOne(profile.instanceId))),
    )
  }, [registry, restoreOne])

  const pair = useCallback(
    async (pairingLink: string, displayName?: string) => {
      setError(null)
      try {
        const paired = await registry.pairAndAdd(
          pairingLink,
          deviceLabel,
          displayName,
          transport,
        )
        syncProfiles()
        onProfilesChanged?.()
        setStates((current) => ({
          ...current,
          [paired.profile.instanceId]: {
            status: 'connected',
            accessToken: paired.accessToken,
            accessExpiresAt: paired.accessExpiresAt,
          },
        }))
      } catch (cause) {
        const message = cause instanceof Error ? cause.message : 'pairing_failed'
        setError(message)
        throw cause
      }
    },
    [deviceLabel, onProfilesChanged, registry, syncProfiles, transport],
  )

  const retry = useCallback(
    async (instanceId: string) => {
      setError(null)
      await restoreOne(instanceId)
    },
    [restoreOne],
  )

  const disconnect = useCallback(
    (instanceId: string) => {
      registry.disconnect(instanceId)
      syncProfiles()
      onProfilesChanged?.()
      setStates((current) => {
        const next = { ...current }
        delete next[instanceId]
        return next
      })
    },
    [onProfilesChanged, registry, syncProfiles],
  )

  const value = useMemo(
    () => ({ profiles, states, error, pair, retry, disconnect }),
    [disconnect, error, pair, profiles, retry, states],
  )

  return (
    <ConnectionRuntimeContext.Provider value={value}>
      {children}
    </ConnectionRuntimeContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components
export function useConnectionRuntime(): ConnectionRuntimeValue {
  const value = useContext(ConnectionRuntimeContext)
  if (!value) throw new Error('ConnectionRuntimeProvider is required')
  return value
}
