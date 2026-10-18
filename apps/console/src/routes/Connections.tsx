import { ConnectionDataFacts } from '../components/ConnectionDataFacts'
import { ControlReadLifetime, observeControl, mergeControlObservation, currentControlObservation, type ControlObservation } from '../connections/controlObservation'
import type { FleetServerReadModel } from '../fleet/readModel'
import type { BrowserDiagnosticJournal } from '../diagnostics/journal'
import { type FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { ConsoleHttpError } from '../api/client'
import { ConsoleContractError } from '../api/contracts'
import type { ManagedFleetControlReadModel, ManagedFleetMeshReadModel, ManagedFleetMutationResult, ManagedFleetNodeReadModel } from '../api/models'
import { useConnectionRuntime } from '../connections/runtime'
import { inspectCanonicalPairingLink } from '../connections/pairingLink'
import { connectionLifecycle, pairingErrorMessageKey, type ConnectionLifecycle } from '../connections/lifecycle'
import { beginMeshCreate, finishMeshCreate, readPendingMeshCreate } from '../connections/meshCreate'
import { isConverged, memberOperationalState, membershipTransitionPending, nativeTrustReady, observedMembership, resolvedMembershipMutation, MembershipMutationLifetime, type MembershipMutation, type MembershipProjection, type MutationPhase, type OperationalState } from '../connections/membershipReadiness'
import { isAutonomous, joinPreflightError } from '../connections/joinPreflight'
import { beginMeshHandoff, finishMeshHandoff, readMeshHandoff } from '../connections/meshHandoff'
import { changeMeshPrimary } from '../connections/meshPrimary'
import { controlNamespace, clearCachedFleetControl, isFleetControlRevisionRegression, loadCachedFleetControl, propagateCachedFleetControl, saveCachedFleetControl, type FleetControlFreshness } from '../connections/controlState'
import type { ConnectionProfile } from '../connections/types'
import type { MessageKey } from '../i18n/catalogs'
import { useI18n } from '../i18n/useI18n'
import { ConfirmationDialog, FeedbackState, IconButton, IconButtonRow } from '../components/UiPrimitives'
import { returnToState } from '../navigation/context'
import { meshSlotsRoute, meshRoute, serverRoute } from '../navigation/routes'
import { MeshVpnPanel } from './MeshVpnPanel'



function lifecycleLabel(
  lifecycle: ConnectionLifecycle,
  t: (key: MessageKey) => string,
): string {
  switch (lifecycle) {
    case 'connecting':
      return t('connections.lifecycle.connecting')
    case 'live':
      return t('connections.status.connected')
    case 'reconnecting':
      return t('connections.lifecycle.reconnecting')
    case 'stale':
      return t('connections.lifecycle.stale')
    case 'offline':
      return t('connections.lifecycle.offline')
    case 'unpaired':
      return t('connections.status.unpaired')
    case 'revoked':
      return t('connections.status.revoked')
    case 'expired':
      return t('connections.status.expired')
    case 'attention':
      return t('connections.actionRequired')
    default:
      return t('connections.status.stored')
  }
}

/** Never display raw HTTP transport messages or token-bearing exception text. */
function controlErrorCode(cause: unknown): string {
  if (cause instanceof ConsoleContractError) return 'control_contract_invalid'
  const value = cause && typeof cause === 'object' && 'code' in cause ? (cause as { code: unknown }).code : undefined
  if (typeof value === 'string' && /^[a-zA-Z0-9_:-]{1,120}$/.test(value)) return value
  if (cause instanceof Error && cause.message === 'mesh_create_reconcile_required') return cause.message
  if (cause instanceof Error && cause.message === 'mesh_primary_handoff_pending') return cause.message
  if (cause instanceof Error && cause.message === 'storage_unavailable') return cause.message
  return 'control_request_failed'
}

function recoveryKey(error: string): MessageKey {
  if (membershipTransitionPending(error)) return 'connections.recovery.transition'
  if (error === 'mesh_primary_handoff_pending') return 'connections.recovery.primary'
  if (error.includes('fleet_id_mismatch')) return 'connections.recovery.fleet'
  if (error.includes('management_contract') || error.includes('contract_')) return 'connections.recovery.contract'
  if (error === 'mesh_create_reconcile_required') return 'connections.recovery.pendingCreate'
  if (error === 'cross_authority_handoff_required') return 'connections.recovery.handoff'
  if (error.includes('enrollment_') || error.includes('pairing_') || error === 'control_write_unavailable') return 'connections.recovery.pair'
  if (error.includes('revision_conflict') || error.includes('already_managed')) return 'connections.recovery.conflict'
  return 'connections.recovery.unknown'
}
type AuthorityView = {
  control: ManagedFleetControlReadModel
  freshness: FleetControlFreshness
  observedAt?: number
  profile?: ConnectionProfile
}

function normalizedOrigin(value: string | undefined): string {
  return (value ?? '').replace(/\/+$/, '').toLowerCase()
}

function nodeForProfile(
  profile: ConnectionProfile,
  control: ManagedFleetControlReadModel | undefined,
  observed: ControlObservation | undefined,
): ManagedFleetNodeReadModel | undefined {
  if (!control) return undefined
  const observedNodeId = observed?.control?.fleetId === control.fleetId
    ? observed.control.nodeId
    : undefined
  const byId = observedNodeId
    ? control.nodes.find((node) => node.nodeId === observedNodeId && node.state !== 'detached')
    : undefined
  if (byId) return byId
  const origin = normalizedOrigin(profile.origin)
  return control.nodes.find((node) => (
    node.state !== 'detached' && normalizedOrigin(node.origin) === origin
  ))
}

function operationalLabel(state: OperationalState, t: (key: MessageKey) => string): string {
  if (state === 'current') return t('connections.current')
  if (state === 'applying') return t('connections.applying')
  if (state === 'attention') return t('connections.actionRequired')
  return t('connections.unavailable')
}

export function Connections({ servers = [], diagnostics }: { servers?: FleetServerReadModel[]; diagnostics?: BrowserDiagnosticJournal } = {}) {
  const { profiles, states, pair, retry, disconnect, client } = useConnectionRuntime()
  const { meshId: routeMeshId } = useParams()
  const { t, number } = useI18n()
  const [pairingLink, setPairingLink] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [pairingFeedback, setPairingFeedback] = useState<{ key: MessageKey; error: boolean } | null>(null)
  const [storedControls, setControls] = useState<Record<string, ControlObservation>>(() => (
    Object.fromEntries(profiles.map((profile) => {
      const cached = loadCachedFleetControl(profile.instanceId)
      return [profile.instanceId, cached
        ? { control: cached.control, freshness: 'stale' as const, observedAt: cached.observedAt }
        : { freshness: 'unknown' as const }]
    }))
  ))
  const [clock, setClock] = useState(() => Date.now())
  const [controlLifetime] = useState(() => new ControlReadLifetime())
  const controls = useMemo(() => Object.fromEntries(Object.entries(storedControls).map(([id, value]) => [id, currentControlObservation(value, clock)])), [storedControls, clock])
  useEffect(() => {
    controlLifetime.activate()
    const timer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => { controlLifetime.deactivate(); window.clearInterval(timer) }
  }, [controlLifetime])
  const [meshName, setMeshName] = useState('Mesh')
  const [newMeshName, setNewMeshName] = useState('Mesh')
  const [newMeshControlInstanceId, setNewMeshControlInstanceId] = useState('')
  const [selectedMeshId, setSelectedMeshId] = useState('')
  const [primaryTargets, setPrimaryTargets] = useState<Record<string, string>>({})
  // Independent control authorities must never share a UI write lock.
  const [busyAuthorities, setBusyAuthorities] = useState<Record<string, number>>({})
  const markBusy = useCallback((instanceId: string, change: number) => {
    setBusyAuthorities((previous) => {
      const value = Math.max(0, (previous[instanceId] ?? 0) + change)
      return { ...previous, [instanceId]: value }
    })
  }, [])
  const busyFor = (instanceId: string | undefined) => Boolean(instanceId && busyAuthorities[instanceId])
  const [controlError, setControlError] = useState<string | null>(null)
  const [optimisticMeshes, setOptimisticMeshes] = useState<ManagedFleetMeshReadModel[] | null>(null)
  const [meshMutationPhase, setMeshMutationPhase] = useState<MutationPhase | null>(null)
  const [membershipMutations, setMembershipMutations] = useState<Record<string, MembershipMutation>>({})
  const [membershipInFlight] = useState(() => new MembershipMutationLifetime())
  const [membershipTargets, setMembershipTargets] = useState<Record<string, string>>({})

  useEffect(() => {
    const handle = window.setTimeout(() => {
      for (const profile of profiles) {
        let pending
        try { pending = readMeshHandoff(localStorage, profile.instanceId) } catch { continue }
        if (!pending) continue
        const observed = controls[profile.instanceId]
        const local = observed?.freshness === 'fresh' ? observed.control : undefined
        const node = local?.nodes.find((item) => item.nodeId === pending.nodeId)
        if (local?.managed && node?.state === 'active'
          && node.meshId === pending.targetMeshId && isConverged(node) && nativeTrustReady(local, pending.targetMeshId)) {
          finishMeshHandoff(localStorage, profile.instanceId, pending.targetMeshId)
          setMembershipMutations((value) => ({ ...value, [profile.instanceId]: { targetMeshId: pending.targetMeshId, phase: 'confirmed' } }))
        } else {
          setMembershipTargets((value) => ({ ...value, [profile.instanceId]: pending.targetMeshId }))
          setMembershipMutations((value) => value[profile.instanceId] ? value : ({ ...value, [profile.instanceId]: { targetMeshId: pending.targetMeshId, phase: 'failed', error: 'control_commit_uncertain' } }))
        }
      }
    }, 0)
    return () => window.clearTimeout(handle)
  }, [profiles, controls])

  // Recover a confirmed Create Mesh after navigation, timeout or process restart.
  useEffect(() => {
    const handle = window.setTimeout(() => {
      for (const profile of profiles) {
        let pending
        try { pending = readPendingMeshCreate(localStorage, profile.instanceId) } catch { continue }
        if (!pending) continue
        const observed = controls[profile.instanceId]
        const control = observed?.freshness === 'fresh' ? observed.control : undefined
        if (!control || control.nodeId !== pending.controlNodeId || control.controlNodeId !== pending.controlNodeId) continue
        const mesh = control.meshes.find((item) => item.meshId === pending.meshId && item.displayName === pending.displayName)
        const node = control.nodes.find((item) => item.nodeId === pending.controlNodeId)
        if (mesh && node?.meshId === pending.meshId && isConverged(node) && nativeTrustReady(control, pending.meshId)) {
          finishMeshCreate(localStorage, profile.instanceId, pending.meshId)
          setMeshMutationPhase('confirmed')
          setControlError(null)
        }
      }
    }, 0)
    return () => window.clearTimeout(handle)
  }, [controls, profiles])
  const [confirmation, setConfirmation] = useState<{ kind: 'delete-mesh'; label: string } | { kind: 'remove-connection'; instanceId: string; label: string } | null>(null)

  const refreshControls = useCallback(async () => {
    const epoch = controlLifetime.begin()
    try { await Promise.all(profiles.map(async (profile) => {
      const api = client(profile.instanceId)
      const observation = api ? await observeControl(() => api.fleetControl())
        : { freshness: 'unknown' as const, error: 'control_client_unavailable' }
      if (!controlLifetime.isCurrent(epoch)) return
      if (observation.control && !isFleetControlRevisionRegression(observation.control, loadCachedFleetControl(profile.instanceId)?.control)) {
        saveCachedFleetControl(profile.instanceId, observation.control, observation.observedAt)
        propagateCachedFleetControl(observation.control, observation.observedAt)
      }
      // Publish each server independently, so one slow Mesh participant cannot hold all others.
      setControls((current) => ({ ...current, [profile.instanceId]: mergeControlObservation(current[profile.instanceId], observation) }))
      if (observation.error && api) diagnostics?.append({ type: 'request_failed', instanceId: profile.instanceId,
        code: observation.error, detail: `/actions/fleet/control status=${observation.status ?? 'unknown'} elapsed_ms=${observation.elapsedMs ?? 0}${observation.contractPath ? ' contract_path=' + observation.contractPath : ''}` })
    })) } finally { controlLifetime.finish() }
  }, [client, profiles, diagnostics, controlLifetime])

  useEffect(() => {
    const handle = window.setTimeout(() => {
      void refreshControls()
    }, 0)
    const recover = () => { if (document.visibilityState !== 'hidden' && controlLifetime.idle()) void refreshControls() }
    const timer = window.setInterval(recover, 15_000)
    document.addEventListener('visibilitychange', recover)
    window.addEventListener('online', recover)
    return () => {
      window.clearTimeout(handle); window.clearInterval(timer)
      document.removeEventListener('visibilitychange', recover); window.removeEventListener('online', recover)
    }
  }, [refreshControls, controlLifetime])

  const authorityViews = useMemo(() => {
    const grouped = new Map<string, ControlObservation & { control: ManagedFleetControlReadModel }>()
    const score = (item: ControlObservation & { control: ManagedFleetControlReadModel }) =>
      (item.control.nodeId === item.control.controlNodeId ? 100 : 0)
      + (item.freshness === 'fresh' ? 10 : item.freshness === 'stale' ? 1 : 0)
      + (item.observedAt ?? 0) / 1e15

    for (const profile of profiles) {
      const observation = controls[profile.instanceId]
      if (!observation?.control?.managed) continue
      const key = controlNamespace(observation.control)
      const current = grouped.get(key)
      if (!current || observation.control.revisions.topology > current.control.revisions.topology
        || observation.control.revisions.topology === current.control.revisions.topology && score(observation as ControlObservation & { control: ManagedFleetControlReadModel }) > score(current)) {
        grouped.set(key, observation as ControlObservation & { control: ManagedFleetControlReadModel })
      }
    }

    return Array.from(grouped.values()).map((observation): AuthorityView => {
      const control = observation.control
      const authorityNode = control.nodes.find((node) => node.nodeId === control.controlNodeId)
      const profile = profiles.find((candidate) => (
        states[candidate.instanceId]?.status === 'connected'
        && (
          controls[candidate.instanceId]?.control?.nodeId === control.controlNodeId
          || candidate.instanceId === control.controlNodeId
          || Boolean(authorityNode?.origin && normalizedOrigin(candidate.origin) === normalizedOrigin(authorityNode.origin))
        )
      ))
      const pairedMember = profile ?? profiles.find((candidate) => {
        const local = controls[candidate.instanceId]?.control
        return states[candidate.instanceId]?.status === 'connected' && local
          && controlNamespace(local) === controlNamespace(control)
          && control.nodes.some((node) => node.nodeId === local.nodeId && node.state === 'active')
      })
      return { control, freshness: observation.freshness, observedAt: observation.observedAt, profile: pairedMember }
    })
  }, [controls, profiles, states])

  const authorityForMesh = useCallback((meshId: string | undefined): AuthorityView | undefined => (
    meshId ? authorityViews.find((view) => view.control.meshes.some((mesh) => mesh.meshId === meshId)) : undefined
  ), [authorityViews])

  const authorityForProfile = useCallback((profile: ConnectionProfile): AuthorityView | undefined => {
    const observed = controls[profile.instanceId]?.control
    if (observed?.managed) {
      const exact = authorityViews.find((view) => (
        controlNamespace(view.control) === controlNamespace(observed)
      ))
      if (exact) return exact
    }
    return authorityViews.find((view) => Boolean(nodeForProfile(profile, view.control, controls[profile.instanceId])))
  }, [authorityViews, controls])

  const managedMeshes = useMemo(() => {
    const seen = new Set<string>()
    const values: ManagedFleetMeshReadModel[] = []
    for (const view of authorityViews) {
      for (const mesh of view.control.meshes) {
        if (seen.has(mesh.meshId)) continue
        seen.add(mesh.meshId)
        values.push(mesh)
      }
    }
    return values
  }, [authorityViews])
  const meshes = optimisticMeshes ?? managedMeshes

  const selectedMesh = useMemo(() => {
    const requested = routeMeshId ?? selectedMeshId
    return meshes.find((mesh) => mesh.meshId === requested) ?? (routeMeshId ? undefined : meshes[0])
  }, [meshes, routeMeshId, selectedMeshId])
  const selectedAuthority = authorityForMesh(selectedMesh?.meshId)
  const authoritative = selectedAuthority?.control
  const authoritativeFreshness = selectedAuthority?.freshness ?? 'unknown'
  const writeProfile = selectedAuthority?.profile
  const selectedMembers = (
    selectedMesh && authoritative
      ? authoritative.nodes.filter((node) => node.meshId === selectedMesh.meshId && node.state !== 'detached')
      : []
  )
  const meshOperationalState: OperationalState = (() => {
    if (meshMutationPhase === 'failed') return 'attention'
    if (meshMutationPhase === 'pending') return 'applying'
    if (!selectedMesh || !authoritative || authoritativeFreshness !== 'fresh') return 'unavailable'
    if (controlError || selectedMembers.some((node) => Boolean(node.lastError) && !membershipTransitionPending(node.lastError))) return 'attention'
    if (selectedMembers.some((node) => !isConverged(node)) || !nativeTrustReady(authoritative, selectedMesh.meshId)) return 'applying'
    return 'current'
  })()

  useEffect(() => {
    const handle = window.setTimeout(() => {
      if (!selectedMesh) {
        if (selectedMeshId) setSelectedMeshId('')
        return
      }
      if (!routeMeshId && selectedMeshId !== selectedMesh.meshId) {
        setSelectedMeshId(selectedMesh.meshId)
      }
      setMeshName(selectedMesh.displayName)
    }, 0)
    return () => window.clearTimeout(handle)
  }, [routeMeshId, selectedMesh, selectedMeshId])

  const mutateControl = useCallback(
    async (authority: AuthorityView | undefined, path: string, body: Record<string, unknown> = {}): Promise<ManagedFleetMutationResult> => {
      const profile = authority?.profile
      if (!profile) {
        const failure = { ok: false, error: 'control_write_unavailable' }
        setControlError(failure.error)
        return failure
      }
      const api = client(profile.instanceId)
      if (!api) {
        const failure = { ok: false, error: 'control_write_unavailable' }
        setControlError(failure.error)
        return failure
      }
      markBusy(profile.instanceId, 1)
      setControlError(null)
      try {
        const result = await api.fleetControlMutation(path, body)
        if (!result.ok) {
          setControlError(result.code ?? result.error ?? 'control_mutation_failed')
          return result
        }
        if (result.control) {
          const observedAt = Date.now()
          saveCachedFleetControl(profile.instanceId, result.control, observedAt)
          propagateCachedFleetControl(result.control, observedAt)
          setControls((current) => ({
            ...current,
            [profile.instanceId]: { control: result.control, freshness: 'fresh', observedAt },
          }))
        }
        void refreshControls()
        return result
      } catch (cause) {
        const failure = {
          ok: false,
          code: cause instanceof ConsoleHttpError && (cause.status === 0 || cause.status >= 500)
            ? 'control_commit_uncertain' : undefined,
          error: controlErrorCode(cause),
        }
        setControlError(failure.error)
        return failure
      } finally {
        markBusy(profile.instanceId, -1)
      }
    },
    [client, markBusy, refreshControls],
  )

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const oneTimeLink = pairingLink
    setPairingLink('')
    setPairingFeedback(null)
    setSubmitting(true)
    try {
      const target = inspectCanonicalPairingLink(oneTimeLink)
      const existing = profiles.find(
        (profile) => normalizedOrigin(profile.origin) === normalizedOrigin(target.origin),
      )
      // A new valid one-time link may re-authorize a revoked/stale saved profile.
      // Pairing registry replaces credentials atomically after successful exchange.
      await pair(oneTimeLink, displayName || undefined)
      if (existing) {
        clearCachedFleetControl(existing.instanceId)
        setControls((current) => ({ ...current, [existing.instanceId]: { freshness: 'unknown' } }))
        void refreshControls()
      }
      setDisplayName('')
      setPairingFeedback({ key: 'connections.pairingSuccess', error: false })
    } catch (cause) {
      setPairingFeedback({ key: pairingErrorMessageKey(cause), error: true })
    } finally {
      setSubmitting(false)
    }
  }

  const membershipFor = useCallback((profile: ConnectionProfile): MembershipProjection => (
    observedMembership(controls[profile.instanceId], authorityForProfile(profile))
  ), [authorityForProfile, controls])

  useEffect(() => {
    const confirmed = profiles.flatMap((profile) => {
      const prior = membershipMutations[profile.instanceId]
      const next = resolvedMembershipMutation(membershipFor(profile), prior)
      return next && next !== prior ? [{ instanceId: profile.instanceId, prior, next }] : []
    })
    if (!confirmed.length) return
    const handle = window.setTimeout(() => {
      setMembershipMutations((current) => {
        const next = { ...current }
        for (const item of confirmed) {
          if (current[item.instanceId] === item.prior) next[item.instanceId] = item.next
        }
        return next
      })
      setControlError((current) => confirmed.some((item) => item.prior?.error === current) ? null : current)
    }, 0)
    return () => window.clearTimeout(handle)
  }, [membershipFor, membershipMutations, profiles])

  const eligibleControlProfiles = useMemo(() => profiles.filter((profile) => {
    if (states[profile.instanceId]?.status !== 'connected') return false
    const membership = membershipFor(profile)
    return membership.kind === 'standalone' && membership.freshness === 'fresh'
  }), [membershipFor, profiles, states])

  useEffect(() => {
    const preferred = eligibleControlProfiles[0]?.instanceId ?? ''
    if (
      newMeshControlInstanceId
      && eligibleControlProfiles.some((profile) => profile.instanceId === newMeshControlInstanceId)
    ) return
    const handle = window.setTimeout(() => setNewMeshControlInstanceId(preferred), 0)
    return () => window.clearTimeout(handle)
  }, [eligibleControlProfiles, newMeshControlInstanceId])

  async function createMesh() {
    const displayName = newMeshName.trim() || 'Mesh'
    const selectedProfile = eligibleControlProfiles.find((profile) => profile.instanceId === newMeshControlInstanceId)
    if (!selectedProfile || states[selectedProfile.instanceId]?.status !== 'connected') {
      setControlError('control_write_unavailable')
      setMeshMutationPhase('failed')
      return
    }
    const api = client(selectedProfile.instanceId)
    if (!api) {
      setControlError('control_write_unavailable')
      setMeshMutationPhase('failed')
      return
    }
    const pendingMesh: ManagedFleetMeshReadModel = {
      meshId: 'pending:' + Date.now(),
      displayName,
      adopted: false,
      updatedAt: new Date().toISOString(),
    }
    setOptimisticMeshes([...meshes, pendingMesh])
    setMeshMutationPhase('pending')
    markBusy(selectedProfile.instanceId, 1)
    setControlError(null)
    let result: ManagedFleetMutationResult
    let requestedMeshId = ''
    let requestedNodeId = ''
    try {
      const enrollment = await api.fleetEnrollment()
      const pending = beginMeshCreate(localStorage, selectedProfile.instanceId, enrollment.nodeId, displayName)
      requestedMeshId = pending.meshId
      requestedNodeId = enrollment.nodeId
      // Resolve a possibly committed lost response before sending another write.
      const current = await api.fleetControl()
      const alreadyCreated = current.nodeId === enrollment.nodeId
        && current.controlNodeId === enrollment.nodeId
        && current.meshes.some((mesh) => mesh.meshId === pending.meshId && mesh.displayName === displayName)
        && current.nodes.some((node) => node.nodeId === enrollment.nodeId && node.meshId === pending.meshId && node.state !== 'detached')
      result = alreadyCreated ? { ok: true, control: current } : await api.fleetControlMutation('/actions/fleet/control/adopt', {
        mesh_id: pending.meshId,
        display_name: displayName,
        control_node_id: enrollment.nodeId,
      })
      if (result.ok && result.control?.meshes.some((mesh) => mesh.meshId === pending.meshId && mesh.displayName === displayName)
        && isConverged(result.control.nodes.find((node) => node.nodeId === enrollment.nodeId && node.meshId === pending.meshId))
        && nativeTrustReady(result.control, pending.meshId)) {
        finishMeshCreate(localStorage, selectedProfile.instanceId, pending.meshId)
      }
      if (result.ok && result.control) {
        const observedAt = Date.now()
        saveCachedFleetControl(selectedProfile.instanceId, result.control, observedAt)
        propagateCachedFleetControl(result.control, observedAt)
        setControls((current) => ({
          ...current,
          [selectedProfile.instanceId]: { control: result.control, freshness: 'fresh', observedAt },
        }))
        void refreshControls()
      } else if (!result.ok) {
        setControlError(result.code ?? result.error ?? 'control_mutation_failed')
      }
    } catch (cause) {
      result = { ok: false, error: controlErrorCode(cause) }
      setControlError(result.error ?? 'control_mutation_failed')
    } finally {
      markBusy(selectedProfile.instanceId, -1)
    }
    setOptimisticMeshes(null)
    setMeshMutationPhase(result.ok && result.control?.meshes.some((mesh) => mesh.meshId === requestedMeshId)
      && isConverged(result.control.nodes.find((node) => node.nodeId === requestedNodeId))
      && nativeTrustReady(result.control, requestedMeshId)
      ? 'confirmed' : result.ok ? 'pending' : 'failed')
    if (result.ok && result.control?.meshes.some((mesh) => mesh.meshId === requestedMeshId)
      && isConverged(result.control.nodes.find((node) => node.nodeId === requestedNodeId))
      && nativeTrustReady(result.control, requestedMeshId)) setNewMeshName('Mesh')
  }

  async function renameMesh() {
    if (!selectedAuthority?.control.managed || !selectedMesh) return
    const displayName = meshName.trim() || selectedMesh.displayName
    setOptimisticMeshes(meshes.map((mesh) => (
      mesh.meshId === selectedMesh.meshId ? { ...mesh, displayName } : mesh
    )))
    setMeshMutationPhase('pending')
    const result = await mutateControl(selectedAuthority, '/actions/fleet/control/mesh/rename', {
      mesh_id: selectedMesh.meshId,
      display_name: displayName,
      expected_topology_revision: selectedAuthority.control.revisions.topology,
    })
    setOptimisticMeshes(null)
    setMeshMutationPhase(result.ok && result.control?.meshes.some((mesh) => mesh.meshId === selectedMesh.meshId && mesh.displayName === displayName) ? 'confirmed' : result.ok ? 'pending' : 'failed')
  }

  async function deleteMesh() {
    if (!selectedAuthority?.control.managed || !selectedMesh) return
    setOptimisticMeshes(meshes.filter((mesh) => mesh.meshId !== selectedMesh.meshId))
    setMeshMutationPhase('pending')
    const result = await mutateControl(selectedAuthority, '/actions/fleet/control/mesh/delete', {
      mesh_id: selectedMesh.meshId,
      expected_topology_revision: selectedAuthority.control.revisions.topology,
    })
    setOptimisticMeshes(null)
    setMeshMutationPhase(result.ok && result.control && !result.control.meshes.some((mesh) => mesh.meshId === selectedMesh.meshId) ? 'confirmed' : result.ok ? 'pending' : 'failed')
  }

  const addToMesh = useCallback(async (instanceId: string, meshId: string): Promise<ManagedFleetMutationResult> => {
    const targetAuthority = authorityForMesh(meshId)
    if (!targetAuthority?.control.managed) return { ok: false, error: 'control_write_unavailable' }
    const targetApi = client(instanceId)
    if (!targetApi) {
      const failure = { ok: false, error: 'enrollment_unavailable' }
      setControlError(failure.error)
      return failure
    }
    setControlError(null)
    try {
      const authorityApi = targetAuthority.profile ? client(targetAuthority.profile.instanceId) : null
      if (!authorityApi) return { ok: false, error: 'control_write_unavailable' }
      const [targetControl, authorityControl] = await Promise.all([targetApi.fleetControl(), authorityApi.fleetControl()])
      const handoff = readMeshHandoff(localStorage, instanceId)
      if (handoff && (handoff.nodeId !== targetControl.nodeId || handoff.targetMeshId !== meshId)) {
        return { ok: false, code: 'control_commit_uncertain', error: 'control_commit_uncertain' }
      }
      if (!authorityControl.managed || !authorityControl.meshes.some((mesh) => mesh.meshId === meshId && mesh.adopted)) {
        return { ok: false, code: 'membership_unknown', error: 'membership_unknown' }
      }
      if (handoff) beginMeshHandoff(localStorage, instanceId, { ...handoff,
        targetControlId: authorityControl.controlNodeId, targetFleetId: authorityControl.fleetId })
      const preflightError = joinPreflightError(targetControl, authorityControl)
      if (preflightError) {
        const failure = { ok: false, code: preflightError, error: preflightError }
        setControlError(failure.error)
        return failure
      }
      const existing = authorityControl.nodes.find((node) => node.nodeId === targetControl.nodeId && node.state !== 'detached' && node.meshId === meshId)
      if (existing) {
        void refreshControls()
        return { ok: true, control: authorityControl }
      }
      const enrollment = await targetApi.fleetEnrollment()
      if (enrollment.nodeId !== targetControl.nodeId) {
        const failure = { ok: false, code: 'enrollment_identity_changed', error: 'enrollment_identity_changed' }
        setControlError(failure.error)
        return failure
      }
      const result = await mutateControl(targetAuthority, '/actions/fleet/control/nodes/upsert', {
        node_id: enrollment.nodeId,
        mesh_id: meshId,
        origin: enrollment.origin,
        public_key: enrollment.publicKey,
        auth_token: enrollment.authToken,
        expected_topology_revision: authorityControl.revisions.topology,
      })
      if (!result.ok && /^(mesh_transition_pending|control_commit_uncertain|http_5\d\d|network_error|timeout|request_timeout|direct_authority_auth_transient)$/.test(result.code ?? result.error ?? '')) {
        // POST may have committed. Reconcile by GET; never automatically replay it.
        try {
          const control = await authorityApi.fleetControl()
          const node = control.nodes.find((item) => item.nodeId === enrollment.nodeId && item.state !== 'detached' && item.meshId === meshId)
          if (node) {
            setControlError(null)
            void refreshControls()
            return { ok: true, control }
          }
        } catch { /* Keep the uncertain receipt; later fresh reads may resolve it. */ }
        void refreshControls()
        if (membershipTransitionPending(result.code ?? result.error)) return result
        setControlError('control_commit_uncertain')
        return { ok: false, code: 'control_commit_uncertain', error: 'control_commit_uncertain' }
      }
      return result
    } catch (cause) {
      const failure = { ok: false, error: controlErrorCode(cause) }
      setControlError(failure.error)
      return failure
    }
    }, [authorityForMesh, client, mutateControl, refreshControls])
  async function transferPrimary(authority: AuthorityView, meshId: string, nodeId: string): Promise<ManagedFleetMutationResult> {
    const api = authority.profile ? client(authority.profile.instanceId) : null
    if (!api) return { ok: false, error: 'control_write_unavailable' }
    try {
      const result = await changeMeshPrimary({ fleetControl: () => api.fleetControl(),
        fleetControlMutation: (path, body) => mutateControl(authority, path, body),
      }, localStorage, meshId, nodeId)
      if (!result.ok) setControlError(result.code ?? result.error ?? 'mesh_primary_handoff_pending')
      void refreshControls()
      return result
    } catch (cause) {
      const failure = { ok: false, error: controlErrorCode(cause) }
      setControlError(failure.error)
      return failure
    }
  }

  async function changeSelectedPrimary() {
    if (!selectedAuthority || !selectedMesh) return
    const target = primaryTargets[selectedMesh.meshId] ?? selectedAuthority.control.controlNodeId
    setMeshMutationPhase('pending')
    const result = await transferPrimary(selectedAuthority, selectedMesh.meshId, target)
    setMeshMutationPhase(result.ok ? 'confirmed' : 'failed')
  }

  async function authorityBeforeDeparture(authority: AuthorityView, nodeId: string): Promise<{ authority?: AuthorityView; failure?: ManagedFleetMutationResult }> {
    const api = authority.profile ? client(authority.profile.instanceId) : null
    if (!api) return { failure: { ok: false, error: 'control_write_unavailable' } }
    const control = await api.fleetControl()
    const node = control.nodes.find((item) => item.nodeId === nodeId && item.state !== 'detached')
    if (!node?.meshId) return { failure: { ok: false, error: 'membership_unknown' } }
    const current = { ...authority, control, freshness: 'fresh' as const }
    const meshId = node.meshId
    const remaining = control.nodes.filter((item) => item.nodeId !== nodeId && item.meshId === node.meshId && item.state === 'active')
    if (control.controlNodeId !== nodeId || remaining.length === 0) return { authority: current }
    const selected = remaining.find((item) => item.nodeId === primaryTargets[meshId])
      ?? [...remaining].sort((a, b) => a.nodeId.localeCompare(b.nodeId))[0]
    const transferred = await transferPrimary(current, node.meshId, selected.nodeId)
    if (!transferred.ok || !transferred.control || transferred.control.controlNodeId !== selected.nodeId) {
      return { failure: transferred.ok ? { ok: false, error: 'mesh_primary_handoff_pending' } : transferred }
    }
    return { authority: { ...current, control: transferred.control } }
  }

  async function detachFromMesh(instanceId: string): Promise<ManagedFleetMutationResult> {
    const profile = profiles.find((item) => item.instanceId === instanceId)
    if (!profile) return { ok: false, error: 'membership_unknown' }
    const sourceAuthority = authorityForProfile(profile)
    if (!sourceAuthority?.control.managed) return { ok: false, error: 'control_write_unavailable' }
    const node = nodeForProfile(profile, sourceAuthority.control, controls[instanceId])
    if (!node?.meshId) return { ok: false, error: 'membership_unknown' }
    const prepared = await authorityBeforeDeparture(sourceAuthority, node.nodeId)
    if (!prepared.authority) return prepared.failure ?? { ok: false, error: 'mesh_primary_handoff_pending' }
    const result = await mutateControl(prepared.authority, '/actions/fleet/control/nodes/detach', {
      node_id: node.nodeId,
      expected_topology_revision: prepared.authority.control.revisions.topology,
    })
    return result
  }

  async function moveToMesh(instanceId: string, targetMeshId: string): Promise<ManagedFleetMutationResult> {
    const profile = profiles.find((item) => item.instanceId === instanceId)
    if (!profile) return { ok: false, error: 'membership_unknown' }
    const sourceAuthority = authorityForProfile(profile)
    const targetAuthority = authorityForMesh(targetMeshId)
    if (!sourceAuthority?.control.managed || !targetAuthority?.control.managed) {
      return { ok: false, error: 'control_write_unavailable' }
    }
    const node = nodeForProfile(profile, sourceAuthority.control, controls[instanceId])
    if (!node?.meshId) return { ok: false, error: 'membership_unknown' }
    const localApi = client(instanceId)
    const destinationApi = targetAuthority.profile ? client(targetAuthority.profile.instanceId) : null
    const sourceApi = sourceAuthority.profile ? client(sourceAuthority.profile.instanceId) : null
    if (!localApi || !destinationApi || !sourceApi) return { ok: false, error: 'control_write_unavailable' }
    try {
      const [local, destination, source] = await Promise.all([localApi.fleetControl(), destinationApi.fleetControl(), sourceApi.fleetControl()])
      if (local.nodeId !== node.nodeId || source.controlNodeId !== sourceAuthority.control.controlNodeId
        || destination.controlNodeId !== targetAuthority.control.controlNodeId
        || !destination.meshes.some((mesh) => mesh.meshId === targetMeshId && mesh.adopted)) {
        return { ok: false, error: 'enrollment_identity_changed' }
      }
      // A switch must establish Join compatibility before releasing membership.
      // addToMesh repeats this check against fresh reads after Detach.
      if (local.schemaVersion !== destination.schemaVersion) {
        return { ok: false, code: 'management_contract_mismatch', error: 'management_contract_mismatch' }
      }
      // Validate operator enrollment before releasing the old membership.
      // Detach rotates ingress credentials, so Join fetches it again afterwards.
      const authorizedEnrollment = await localApi.fleetEnrollment()
      if (authorizedEnrollment.nodeId !== local.nodeId) return { ok: false, error: 'enrollment_identity_changed' }
      beginMeshHandoff(localStorage, instanceId, { nodeId: node.nodeId,
        sourceControlId: source.controlNodeId, targetControlId: destination.controlNodeId,
        targetFleetId: destination.fleetId, targetMeshId })
      if (local.controlNodeId === destination.controlNodeId) return await addToMesh(instanceId, targetMeshId)
      if (!isAutonomous(local)) {
        if (local.controlNodeId !== source.controlNodeId) return { ok: false, error: 'already_managed_by_other_authority' }
        // Detach uses its own fresh revision. An ambiguous POST is resolved by
        // the target's direct read; never release a second authority speculatively.
        const prepared = await authorityBeforeDeparture({ ...sourceAuthority, control: source }, node.nodeId)
        if (!prepared.authority) return prepared.failure ?? { ok: false, error: 'mesh_primary_handoff_pending' }
        const departure = await mutateControl(prepared.authority, '/actions/fleet/control/nodes/detach', {
          node_id: node.nodeId, expected_topology_revision: prepared.authority.control.revisions.topology,
        })
        if (!departure.ok && !/^(control_commit_uncertain|http_5\d\d|network_error|timeout|request_timeout)$/.test(departure.code ?? departure.error ?? '')) return departure
      }
      const detached = await localApi.fleetControl()
      if (detached.nodeId !== node.nodeId || !isAutonomous(detached)) {
        return { ok: false, code: 'control_commit_uncertain', error: 'control_commit_uncertain' }
      }
      return await addToMesh(instanceId, targetMeshId)
    } catch (cause) {
      return { ok: false, error: controlErrorCode(cause) }
    }
  }

  async function changeMembership(instanceId: string, targetMeshId: string) {
    if (membershipInFlight.has(instanceId)) return
    const profile = profiles.find((item) => item.instanceId === instanceId)
    if (!profile) return
    const current = membershipFor(profile)
    const currentMeshId = current.kind === 'mesh' ? current.meshId ?? '' : ''
    const alreadyAuthoritative = (
      current.kind !== 'unknown'
      && currentMeshId === targetMeshId
      && current.freshness === 'fresh'
      && current.ready === true
    )
    if (alreadyAuthoritative) {
      setMembershipMutations((value) => ({
        ...value,
        [instanceId]: { targetMeshId, phase: 'confirmed' },
      }))
      return
    }

    setMembershipMutations((value) => ({
      ...value,
      [instanceId]: { targetMeshId, phase: 'pending' },
    }))

    membershipInFlight.begin(instanceId)
    let result: ManagedFleetMutationResult
    try {
      if (!targetMeshId) {
        result = current.kind === 'mesh'
          ? await detachFromMesh(instanceId)
          : { ok: false, error: 'membership_unknown' }
      } else if (current.kind === 'mesh' && currentMeshId === targetMeshId) {
        // Read authority before repeating Join: a stale replica may still
        // display the desired Mesh after detach or an uncertain response.
        result = await addToMesh(instanceId, targetMeshId)
      } else if (current.kind === 'mesh') {
        result = await moveToMesh(instanceId, targetMeshId)
      } else if (current.kind === 'standalone') {
        result = await addToMesh(instanceId, targetMeshId)
      } else {
        result = { ok: false, error: 'membership_unknown' }
      }

    } finally { membershipInFlight.finish(instanceId) }

    const error = result.ok ? undefined : result.code ?? result.error ?? 'control_mutation_failed'
    if (membershipTransitionPending(error)) setControlError(null)
    setMembershipMutations((value) => ({
      ...value,
      [instanceId]: {
        targetMeshId,
        phase: result.ok || membershipTransitionPending(error) ? 'pending' : 'failed',
        error,
      },
    }))
    void refreshControls()
  }

  async function rotateTrust(instanceId: string) {
    const observed = controls[instanceId]?.control
    const api = client(instanceId)
    if (!observed?.managed || !api) {
      setControlError('control_write_unavailable')
      return
    }
    markBusy(instanceId, 1)
    setControlError(null)
    try {
      const result = await api.fleetControlMutation('/actions/fleet/control/trust/rotate', {
        expected_trust_revision: observed.revisions.trust,
      })
      if (!result.ok) {
        setControlError(result.code ?? result.error ?? 'control_mutation_failed')
        return
      }
      if (result.control) {
        const observedAt = Date.now()
        saveCachedFleetControl(instanceId, result.control, observedAt)
        propagateCachedFleetControl(result.control, observedAt)
        setControls((current) => ({
          ...current,
          [instanceId]: { control: result.control, freshness: 'fresh', observedAt },
        }))
      }
      void refreshControls()
    } catch (cause) {
      setControlError(controlErrorCode(cause))
    } finally {
      markBusy(instanceId, -1)
    }
  }

  const connectionGroups = [
    ...meshes.map((mesh) => ({
      key: mesh.meshId,
      label: mesh.displayName,
      profiles: profiles.filter((profile) => {
        const membership = membershipFor(profile)
        return membership.kind === 'mesh' && membership.meshId === mesh.meshId
      }),
    })),
    {
      key: 'standalone',
      label: t('connections.standalone'),
      profiles: profiles.filter((profile) => membershipFor(profile).kind === 'standalone'),
    },
    {
      key: 'unknown',
      label: t('connections.unknown'),
      profiles: profiles.filter((profile) => membershipFor(profile).kind === 'unknown'),
    },
  ].filter((group) => group.profiles.length > 0)

  return (
    <section className="stack compact-operational-surface connections-surface" aria-label={routeMeshId ? t('title.mesh') : t('connections.title')}>
      {!routeMeshId ? <div className="page-heading">
        <div>
          <p className="eyebrow">{t('connections.fleetAccess')}</p>
          <h2>{t('connections.title')}</h2>
          <p className="muted">{t('connections.restoreHint')}</p>
        </div>
        <span className="environment-badge">{number(profiles.length)} {t('connections.saved')}</span>
      </div> : null}

      {!routeMeshId ? <form className="surface-section connection-form" onSubmit={onSubmit}>
        <div>
          <p className="eyebrow">{t('connections.addServer')}</p>
          <h3>{t('connections.pairTerminal')}</h3>
        </div>
        <label className="ui-field">
          <span>{t('connections.pairingLink')}</span>
          <input
            required
            type="url"
            placeholder={t('connections.pairingPlaceholder')}
            value={pairingLink}
            onChange={(event) => {
              setPairingLink(event.target.value)
              setPairingFeedback(null)
            }}
          />
        </label>
        <label className="ui-field">
          <span>{t('connections.displayName')}</span>
          <input
            type="text"
            maxLength={120}
            placeholder={t('connections.optional')}
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
          />
        </label>
        <IconButton type="submit" icon="connect" variant="primary" label={t('connections.addServerAction')} busy={submitting} />
        <p
          className={'mutation-status-slot ' + (pairingFeedback?.error ? 'connection-error' : 'muted')}
          role={pairingFeedback?.error ? 'alert' : pairingFeedback ? 'status' : undefined}
          aria-live={pairingFeedback ? 'polite' : undefined}
        >
          {pairingFeedback ? t(pairingFeedback.key) : '\u00a0'}
        </p>
      </form> : null}

      {routeMeshId && !selectedMesh && meshes.length > 0 ? (
        <FeedbackState variant="partial" title={t('connections.unknown')} detail={t('connections.meshState') + ': ' + t('connections.unavailable')} />
      ) : null}

      <article className="surface-section mesh-control">
        <div className="connection-card-heading">
          <div>
            <p className="eyebrow">{t('connections.mesh')}</p>
            <h3>{routeMeshId ? selectedMesh?.displayName ?? t('connections.unknown') : t('fleet.meshes')}</h3>
          </div>
          {routeMeshId && selectedMesh ? (
            <span className={'status mesh-state-' + meshOperationalState}>
              {operationalLabel(meshOperationalState, t)}
            </span>
          ) : null}
        </div>
        <p className="muted">{routeMeshId ? t('connections.manageHint') : t('fleet.meshesHint')}</p>
        {selectedMesh && routeMeshId ? (
          <div className="connection-actions">
            <Link className="nav-link" to={meshSlotsRoute(selectedMesh.meshId)}>{t('nav.slots')}</Link>
          </div>
        ) : null}
        {routeMeshId && selectedMesh ? (
          <div className="mesh-operational-summary">
            <span>{t('connections.meshState')}: {operationalLabel(meshOperationalState, t)}</span>
            <span>{t('connections.members')}: {number(selectedMembers.length)}</span>
          </div>
        ) : null}
        {routeMeshId && selectedMesh && authoritative ? (
          <div className="connection-group-list" aria-label={t('connections.members')}>
            {selectedMembers.filter((node) => !profiles.some((candidate) => nodeForProfile(candidate, authoritative, controls[candidate.instanceId])?.nodeId === node.nodeId)).map((node) => {
              const state = node.lastError ? 'attention' : isConverged(node) ? 'current' : 'applying'
              return (
                <div className="connection-card-heading" key={node.nodeId}>
                  <div>
                    <strong>{node.nodeId}</strong>
                    <p className="muted">{operationalLabel(state, t)}</p>
                  </div>
                </div>
              )
            })}
          </div>
        ) : null}
        {routeMeshId && authoritative?.managed ? (
          <details className="mesh-technical-details">
            <summary>{t('connections.technicalDetails')}</summary>
            <div className="mesh-revisions">
              <span>{t('connections.controlNode')}: {authoritative.controlNodeId}</span>
              <span>{t('connections.topologyRevision')}: {number(authoritative.revisions.topology)}</span>
              <span>{t('connections.trustRevision')}: {number(authoritative.revisions.trust)}</span>
              <span>{t('connections.policyRevision')}: {number(authoritative.revisions.accessPolicy)}</span>
              {controlError ? <span>{controlError}</span> : null}
              {selectedMembers.map((node) => (
                <span key={node.nodeId}>
                  {node.nodeId}: topology {number(node.appliedTopologyRevision)}/{number(node.desiredTopologyRevision)} · trust {number(node.appliedTrustRevision)}/{number(node.desiredTrustRevision)} · policy {number(node.appliedPolicyRevision)}/{number(node.desiredPolicyRevision)}{node.lastError ? ' · ' + node.lastError : ''}
                </span>
              ))}
            </div>
          </details>
        ) : null}

        {!routeMeshId && meshes.length > 0 ? (
          <nav className="mesh-entity-list" aria-label={t('connections.mesh')}>
            {meshes.map((mesh) => (
              <a key={mesh.meshId} className={selectedMesh?.meshId === mesh.meshId ? 'chip active' : 'chip'} href={meshRoute(mesh.meshId)}>
                {mesh.displayName}
              </a>
            ))}
          </nav>
        ) : null}


        {routeMeshId && selectedMesh ? (
          <>
            <label className="ui-field">
              <span>{t('connections.meshName')}</span>
              <input
                type="text"
                maxLength={120}
                value={meshName}
                onChange={(event) => setMeshName(event.target.value)}
              />
            </label>
            <label className="ui-field">
              <span>{t('connections.controlNode')}</span>
              <select aria-label={t('connections.controlNode')} value={primaryTargets[selectedMesh.meshId] ?? authoritative?.controlNodeId ?? ''}
                disabled={busyFor(writeProfile?.instanceId) || !writeProfile}
                onChange={(event) => setPrimaryTargets((value) => ({ ...value, [selectedMesh.meshId]: event.target.value }))}>
                {selectedMembers.filter((node) => node.state === 'active').map((node) => <option key={node.nodeId} value={node.nodeId}>
                  {profiles.find((profile) => nodeForProfile(profile, authoritative, controls[profile.instanceId])?.nodeId === node.nodeId)?.displayName ?? node.nodeId}
                </option>)}
              </select>
            </label>
            <p className="muted">{t('connections.primaryHint')}</p>
            <IconButtonRow className="connection-actions">
              <IconButton icon="save" variant="primary" label={t('connections.changePrimary')} disabled={busyFor(writeProfile?.instanceId) || !writeProfile || !primaryTargets[selectedMesh.meshId] || primaryTargets[selectedMesh.meshId] === authoritative?.controlNodeId} onClick={() => void changeSelectedPrimary()} />
              <IconButton icon="edit" variant="secondary" label={t('connections.renameMesh')} disabled={busyFor(writeProfile?.instanceId) || !writeProfile} onClick={() => void renameMesh()} />
              <IconButton icon="delete" variant="destructive" label={t('connections.deleteMesh')} disabled={busyFor(writeProfile?.instanceId) || !writeProfile} onClick={() => { if (selectedMesh) setConfirmation({ kind: 'delete-mesh', label: selectedMesh.displayName }) }} />
            </IconButtonRow>
          </>
        ) : null}

        {!routeMeshId ? <>
        <label className="ui-field">
          <span>{t('connections.newMeshName')}</span>
          <input
            type="text"
            maxLength={120}
            value={newMeshName}
            onChange={(event) => setNewMeshName(event.target.value)}
          />
        </label>
        <label className="ui-field">
          <span>{t('connections.controlNode')}</span>
          <select
            value={newMeshControlInstanceId}
            disabled={eligibleControlProfiles.length === 0}
            onChange={(event) => setNewMeshControlInstanceId(event.target.value)}
          >
            {eligibleControlProfiles.map((profile) => (
              <option key={profile.instanceId} value={profile.instanceId}>{profile.displayName}</option>
            ))}
          </select>
        </label>
        <div className="connection-actions">
          <IconButton icon="create" variant="primary" label={t('connections.createMesh')} disabled={busyFor(newMeshControlInstanceId) || !newMeshControlInstanceId} onClick={() => void createMesh()} />
        </div>
        </> : null}
        <p className={'mutation-status-slot ' + (controlError || meshMutationPhase === 'failed' ? 'connection-error' : 'muted')} role={controlError ? 'alert' : meshMutationPhase || (routeMeshId && authoritativeFreshness !== 'fresh') ? 'status' : undefined} aria-live={controlError || meshMutationPhase || (routeMeshId && authoritativeFreshness !== 'fresh') ? 'polite' : undefined}>
          {controlError
            ? <>{t('connections.actionRequired')} · {controlError} · {t(recoveryKey(controlError))}</>
            : meshMutationPhase === 'pending'
              ? t('connections.pending')
              : meshMutationPhase === 'confirmed'
                ? t('connections.confirmed')
                : meshMutationPhase === 'failed'
                  ? t('connections.failed')
                  : routeMeshId && authoritativeFreshness !== 'fresh'
                    ? t('connections.syncState') + ': ' + t('connections.unavailable')
                    : '\u00a0'}
        </p>
      </article>

      {confirmation ? <ConfirmationDialog
        title={confirmation.kind === 'delete-mesh' ? t('connections.deleteMesh') : t('connections.remove')}
        subject={confirmation.label}
        detail={confirmation.kind === 'delete-mesh' ? t('connections.deleteMeshConfirm') : t('connections.removeConfirm')}
        cancelLabel={t('connections.handoff.cancel')}
        confirmLabel={confirmation.kind === 'delete-mesh' ? t('connections.deleteMesh') : t('connections.remove')}
        confirmIcon={confirmation.kind === 'delete-mesh' ? 'delete' : 'disconnect'}
        confirmVariant="destructive"
        busy={confirmation.kind === 'delete-mesh' && busyFor(writeProfile?.instanceId)}
        onCancel={() => setConfirmation(null)}
        onConfirm={() => {
          const current = confirmation
          setConfirmation(null)
          if (current.kind === 'delete-mesh') void deleteMesh()
          else disconnect(current.instanceId)
        }}
      /> : null}

      <div className="connection-list">
        {profiles.length === 0 ? (
          <FeedbackState variant="empty" title={t('connections.noPairedServers')} detail={t('connections.usePairingLink')} />
        ) : (
          connectionGroups.map((group) => (
            <section className="connection-group" key={group.key}>
              <div className="connection-group-heading">
                <h3>{group.label}</h3>
                <span className="muted">{number(group.profiles.length)}</span>
              </div>
              <div className="connection-group-list">
                {group.profiles.map((profile) => {
                  const state = states[profile.instanceId]
                  const observed = controls[profile.instanceId]
                  const lifecycle = connectionLifecycle(state, observed)
                  const connectionIsLive = Boolean(state?.status === 'connected' && observed?.freshness === 'fresh' && observed.control && !observed.error && profile.metadata.scope.split(/\s+/).includes('terminal:execute'))
                  const membership = membershipFor(profile)
                  const member = membership.node
                  const memberAuthority = authorityForProfile(profile)
                  const desiredMember = memberAuthority?.freshness === 'fresh'
                    ? nodeForProfile(profile, memberAuthority.control, observed) : undefined
                  const memberError = desiredMember?.lastError ?? member?.lastError
                  const memberFailed = Boolean(memberError && !membershipTransitionPending(memberError))
                  const memberMesh = membership.kind === 'mesh' && membership.meshId
                    ? meshes.find((mesh) => mesh.meshId === membership.meshId)
                    : undefined
                  const membershipMutation = resolvedMembershipMutation(
                    membership,
                    membershipMutations[profile.instanceId],
                  )
                  const memberState = memberOperationalState(membership, membershipMutation, desiredMember)
                  const membershipLabel = membership.kind === 'mesh'
                    ? memberMesh?.displayName ?? membership.meshId ?? t('connections.unknown')
                    : membership.kind === 'standalone'
                      ? t('connections.standalone')
                      : t('connections.unknown')
                  const authoritativeTarget = membership.kind === 'mesh' ? membership.meshId ?? '' : ''
                  const membershipTarget = membershipTargets[profile.instanceId] ?? membershipMutation?.targetMeshId ?? desiredMember?.meshId ?? authoritativeTarget
                  const membershipAuthorityId = (membershipTarget
                    ? authorityForMesh(membershipTarget)?.profile?.instanceId
                    : authorityForProfile(profile)?.profile?.instanceId) ?? profile.instanceId
                  const membershipBusy = busyFor(membershipAuthorityId)
                  return (
                    <article
                      className="panel connection-card"
                      id={'connection-' + profile.instanceId}
                      key={profile.instanceId}
                      tabIndex={-1}
                    >
                      <div className="connection-card-heading">
                        <div>
                          <h3>{routeMeshId ? <Link className="text-link" to={serverRoute(profile.instanceId)} state={returnToState(meshRoute(routeMeshId))}>{profile.displayName}</Link> : profile.displayName}</h3>
                          <p className="muted">{profile.origin}</p>
                        </div>
                        <span className={'status connection-status-' + lifecycle}>
                          {lifecycleLabel(lifecycle, t)}
                        </span>
                      </div>
                      <p className="muted">{t('connections.device')}: {profile.metadata.deviceLabel}</p>
                      <ConnectionDataFacts server={servers.find((item) => item.instanceId === profile.instanceId)} control={observed} canConfigure={connectionIsLive} />
                      <div className="connection-fleet-status">
                        <span>
                          {t('connections.mesh')}: {membership.kind === 'mesh' && membership.meshId && !routeMeshId
                            ? <a className="text-link" href={meshRoute(membership.meshId)}>{membershipLabel}</a>
                            : membershipLabel}{membership.freshness === 'stale' ? ' · ' + t('connections.stale') : ''}
                        </span>
                        <span>{t('connections.reachability')}: {state?.status === 'connected' ? t('connections.authenticated') : lifecycleLabel(lifecycle, t)}</span>
                        {membership.kind === 'mesh' || membershipMutation || desiredMember?.meshId ? (
                          <span>
                            {t('connections.syncState')}: {operationalLabel(memberState, t)}
                          </span>
                        ) : null}
                      </div>
                      {routeMeshId && membership.kind !== 'unknown' && meshes.length > 0 ? (
                        <div className="mesh-membership-control ui-field">
                          <span>{t('connections.authoritativeMembership')}: {membershipLabel}{membership.freshness !== 'fresh' ? ' · ' + t('connections.stale') : ''}</span>
                          <span>{t('connections.targetMembership')}</span>
                          <div className="membership-target-options" role="group" aria-label={t('connections.targetMembership')}>
                            <button
                              type="button"
                              className={membershipTarget === '' ? 'chip active' : 'chip'}
                              aria-pressed={membershipTarget === ''}
                              disabled={membershipBusy || !connectionIsLive || membershipMutation?.phase === 'pending'}
                              onClick={() => setMembershipTargets((value) => ({ ...value, [profile.instanceId]: '' }))}
                            >
                              {t('connections.standalone')}
                            </button>
                            {meshes.map((mesh) => (
                              <button
                                key={mesh.meshId}
                                type="button"
                                className={membershipTarget === mesh.meshId ? 'chip active' : 'chip'}
                                aria-pressed={membershipTarget === mesh.meshId}
                                disabled={membershipBusy || !connectionIsLive || membershipMutation?.phase === 'pending'}
                                onClick={() => setMembershipTargets((value) => ({ ...value, [profile.instanceId]: mesh.meshId }))}
                              >
                                {mesh.displayName}
                              </button>
                            ))}
                          </div>
                          <IconButton className="membership-commit" icon="apply" variant="primary" label={t('connections.applyMembership')} busy={membershipMutation?.phase === 'pending'} disabled={membershipBusy || !connectionIsLive} onClick={() => void changeMembership(profile.instanceId, membershipTarget)} />
                        </div>
                      ) : null}
                      {routeMeshId && membership.kind === 'mesh' && observed?.control?.managed && connectionIsLive ? (
                        <MeshVpnPanel
                          instanceId={profile.instanceId}
                          nodeId={member?.nodeId ?? profile.instanceId}
                          client={client}
                          peers={profiles
                            .filter((candidate) => candidate.instanceId !== profile.instanceId
                              && membershipFor(candidate).kind === 'mesh'
                              && membershipFor(candidate).meshId === membership.meshId)
                            .map((candidate) => ({
                              instanceId: candidate.instanceId,
                              nodeId: membershipFor(candidate).node?.nodeId ?? candidate.instanceId,
                              displayName: candidate.displayName,
                            }))}
                          expectedTopologyRevision={observed.control.revisions.topology}
                        />
                      ) : null}
                      <p className={'mutation-status-slot connection-card-status-slot ' + (membershipMutation?.phase === 'failed' || memberFailed || observed?.error || state?.status === 'error' ? 'connection-error' : 'muted')} role={memberFailed || observed?.error || state?.status === 'error' ? 'alert' : membershipMutation || memberError ? 'status' : undefined} aria-live={memberError || observed?.error || state?.status === 'error' || membershipMutation ? 'polite' : undefined}>
                        {state?.status === 'error'
                          ? t('connections.actionRequired') + (state.retryable ? ' · ' + t('connections.retryAvailable') : '')
                          : observed?.error
                            ? <>{t('connections.controlError')} · <code>/actions/fleet/control</code> · {observed.error}{observed.contractPath ? <> · <code>{observed.contractPath}</code> · {t(recoveryKey(observed.error))}</> : null}{observed.elapsedMs === undefined ? '' : ` · ${observed.elapsedMs} ms`}</>
                            : memberError
                              ? <>{t(memberFailed ? 'connections.actionRequired' : 'connections.applying')} · {memberError} · {t(recoveryKey(memberError))}</>
                              : membershipMutation?.phase === 'pending'
                                ? t('connections.applying')
                                : membershipMutation?.phase === 'confirmed'
                                  ? t('connections.confirmed')
                                  : membershipMutation?.phase === 'failed'
                                    ? <>{t('connections.actionRequired')} · {membershipMutation.error ?? 'control_mutation_failed'} · {t(recoveryKey(membershipMutation.error ?? 'control_mutation_failed'))}</>
                                    : '\u00a0'}
                      </p>
                      <IconButtonRow className="connection-actions">
                        {!routeMeshId && (
                          (state?.status === 'error' && state.retryable)
                          || lifecycle === 'stale'
                          || lifecycle === 'offline'
                        ) ? (
                          <IconButton
                            icon="retry"
                            variant="primary"
                            label={t('connections.retryNow')}
                            onClick={() => void retry(profile.instanceId).then(() => refreshControls())}
                          />
                        ) : null}
                        {routeMeshId && observed?.control?.managed ? (
                          <IconButton icon="rotate" variant="secondary" label={t('connections.rotateTrust')} disabled={busyFor(profile.instanceId) || !connectionIsLive} onClick={() => void rotateTrust(profile.instanceId)} />
                        ) : null}
                        {!routeMeshId ? <IconButton icon="disconnect" variant="destructive" label={t('connections.remove')} onClick={() => setConfirmation({ kind: 'remove-connection', instanceId: profile.instanceId, label: profile.displayName })} /> : null}
                      </IconButtonRow>
                    </article>
                  )
                })}
              </div>
            </section>
          ))
        )}
      </div>
    </section>
  )
}
