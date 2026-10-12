import { connectionAuthorityScope } from './authorityScope'
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
import type { ConsoleClient } from '../api/client'
import { BrowserDirectAuthorityClient } from '../fleet/directAuthority'
import { BrowserConnectionRegistry } from './registry'
import type { BrowserDiagnosticJournal } from '../diagnostics/journal'
import type { ConnectionProfile, ProfileRestoreResult } from './types'

export type RuntimeProfileState =
  | { status: 'restoring' }
  | { status: 'connected'; accessToken: string; accessExpiresAt: number }
  | { status: 'unpaired' | 'revoked' | 'expired' }
  | { status: 'error'; retryable: boolean; message: string }

type ConnectionRuntimeValue = {
  profiles: ConnectionProfile[]
  states: Record<string, RuntimeProfileState>
  error: string | null
  pair: (pairingLink: string, displayName?: string) => Promise<void>
  retry: (instanceId: string) => Promise<void>
  disconnect: (instanceId: string) => void
  client: (instanceId: string) => Pick<ConsoleClient, 'fleetControl' | 'fleetControlMutation' | 'fleetEnrollment' | 'meshVpnStatus' | 'meshVpnMutation'> | null
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
    case 'unpaired':
      return { status: 'unpaired' }
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
  restoreOnMount = true,
  diagnostics,
}: {
  children: ReactNode
  registry?: BrowserConnectionRegistry
  transport?: PairingTransport
  deviceLabel?: string
  onProfilesChanged?: () => void
  restoreOnMount?: boolean
  diagnostics?: BrowserDiagnosticJournal
}) {
  const [registry] = useState(() => registryProp ?? new BrowserConnectionRegistry())
  const [transport] = useState(() => transportProp ?? new PairingTransport())
  const authority = useMemo(() => new BrowserDirectAuthorityClient(registry), [registry])
  const [profiles, setProfiles] = useState<ConnectionProfile[]>(() => registry.list())
  const [states, setStates] = useState<Record<string, RuntimeProfileState>>(() =>
    restoreOnMount
      ? Object.fromEntries(registry.list().map((profile) => [
          profile.instanceId,
          registry.credential(profile.instanceId) ? { status: 'restoring' } : { status: 'unpaired' },
        ])) as Record<string, RuntimeProfileState>
      : {},
  )
  const [error, setError] = useState<string | null>(null)
  const restoreStarted = useRef(false)
  const restoreRetryTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>())
  const restoreRetryAttempts = useRef(new Map<string, number>())

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
    const knownScopes = new Map(registry.list().map((profile) => [profile.instanceId, connectionAuthorityScope(profile)]))
    return registry.subscribeAuth((instanceId, result) => {
      const nextState = stateFromRestore(result)
      if ('profile' in result) {
        const scope = connectionAuthorityScope(result.profile)
        if (scope !== knownScopes.get(instanceId)) {
          knownScopes.set(instanceId, scope)
          syncProfiles()
          onProfilesChanged?.()
        }
      }
      setStates((current) => {
        const next = { ...current }
        if (nextState) next[instanceId] = nextState
        else delete next[instanceId]
        return next
      })
    })
  }, [registry, syncProfiles, onProfilesChanged])

  useEffect(() => {
    if (!restoreOnMount || restoreStarted.current) return
    restoreStarted.current = true
    const current = registry.list()
    void Promise.resolve().then(() =>
      Promise.all(current.map((profile) => restoreOne(profile.instanceId))),
    )
  }, [registry, restoreOnMount, restoreOne])

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
        diagnostics?.append({ type: 'profile_paired', instanceId: paired.profile.instanceId, server: paired.profile.displayName, status: 'connected' })
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
        diagnostics?.append({ type: 'profile_pair_failed', code: message, detail: message })
        setError(message)
        throw cause
      }
    },
    [deviceLabel, diagnostics, onProfilesChanged, registry, syncProfiles, transport],
  )

  const retry = useCallback(
    async (instanceId: string) => {
      setError(null)
      const profile = registry.get(instanceId)
      const timer = restoreRetryTimers.current.get(instanceId)
      if (timer !== undefined) {
        clearTimeout(timer)
        restoreRetryTimers.current.delete(instanceId)
      }
      diagnostics?.append({ type: 'profile_retry', instanceId, server: profile?.displayName })
      await restoreOne(instanceId)
    },
    [diagnostics, registry, restoreOne],
  )

  // Temporary authentication failures (including proxy HTTP 520) must recover
  // without requiring an Android process restart or another pairing.
  useEffect(() => {
    const eligible = new Set<string>()
    for (const [instanceId, state] of Object.entries(states)) {
      if (state.status === 'error' && state.retryable && registry.credential(instanceId)) {
        eligible.add(instanceId)
        if (!restoreRetryTimers.current.has(instanceId)) {
          const attempt = (restoreRetryAttempts.current.get(instanceId) ?? 0) + 1
          restoreRetryAttempts.current.set(instanceId, attempt)
          const delay = Math.min(30_000, 1_000 * 2 ** Math.min(attempt - 1, 5))
          const handle = setTimeout(() => {
            restoreRetryTimers.current.delete(instanceId)
            void restoreOne(instanceId)
          }, delay)
          restoreRetryTimers.current.set(instanceId, handle)
        }
      } else if (state.status !== 'restoring') {
        restoreRetryAttempts.current.delete(instanceId)
      }
    }
    for (const [instanceId, handle] of restoreRetryTimers.current) {
      if (!eligible.has(instanceId)) {
        clearTimeout(handle)
        restoreRetryTimers.current.delete(instanceId)
      }
    }
  }, [registry, restoreOne, states])

  useEffect(() => {
    const timers = restoreRetryTimers.current
    const attempts = restoreRetryAttempts.current
    return () => {
      for (const handle of timers.values()) clearTimeout(handle)
      timers.clear()
      attempts.clear()
    }
  }, [])

  const client = useCallback(
    (instanceId: string): Pick<ConsoleClient, 'fleetControl' | 'fleetControlMutation' | 'fleetEnrollment' | 'meshVpnStatus' | 'meshVpnMutation'> | null => {
      const profile = profiles.find((item) => item.instanceId === instanceId)
      if (!profile || !registry.credential(instanceId)) return null
      return {
        fleetControl: () => authority.fleetControl(instanceId),
        fleetControlMutation: (path, body) => authority.fleetControlMutation(instanceId, path, body ?? {}),
        fleetEnrollment: () => authority.fleetEnrollment(instanceId),
        meshVpnStatus: () => authority.meshVpnStatus(instanceId),
        meshVpnMutation: (action, body) => authority.meshVpnMutation(instanceId, action, body ?? {}),
      }
    },
    [authority, profiles, registry],
  )

  const disconnect = useCallback(
    (instanceId: string) => {
      const profile = registry.get(instanceId)
      registry.disconnect(instanceId)
      const timer = restoreRetryTimers.current.get(instanceId)
      if (timer !== undefined) clearTimeout(timer)
      restoreRetryTimers.current.delete(instanceId)
      restoreRetryAttempts.current.delete(instanceId)
      diagnostics?.append({ type: 'profile_removed', instanceId, server: profile?.displayName })
      syncProfiles()
      onProfilesChanged?.()
      setStates((current) => {
        const next = { ...current }
        delete next[instanceId]
        return next
      })
    },
    [diagnostics, onProfilesChanged, registry, syncProfiles],
  )

  const value = useMemo(
    () => ({ profiles, states, error, pair, retry, disconnect, client }),
    [client, disconnect, error, pair, profiles, retry, states],
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
