import { type FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import type { ManagedFleetControlReadModel, ManagedFleetMeshReadModel, ManagedFleetMutationResult, ManagedFleetNodeReadModel } from '../api/models'
import { useConnectionRuntime } from '../connections/runtime'
import { loadCachedFleetControl, propagateCachedFleetControl, saveCachedFleetControl, type FleetControlFreshness } from '../connections/controlState'
import type { ConnectionProfile } from '../connections/types'
import type { MessageKey } from '../i18n/catalogs'
import { useI18n } from '../i18n/useI18n'
import { FeedbackState } from '../components/UiPrimitives'
import { meshPersistentRoute, meshRoute } from '../navigation/routes'

function statusLabel(status: string | undefined, t: (key: MessageKey) => string): string {
  switch (status) {
    case 'connected':
      return t('connections.status.connected')
    case 'restoring':
      return t('connections.status.restoring')
    case 'unpaired':
      return t('connections.status.unpaired')
    case 'revoked':
      return t('connections.status.revoked')
    case 'expired':
      return t('connections.status.expired')
    case 'error':
      return t('connections.status.reconnectNeeded')
    default:
      return t('connections.status.stored')
  }
}

type ControlObservation = {
  control?: ManagedFleetControlReadModel
  error?: string
  freshness: FleetControlFreshness
  observedAt?: number
}

type MutationPhase = 'pending' | 'confirmed' | 'failed'
type MembershipMutation = { targetMeshId: string; phase: MutationPhase; error?: string }
type MembershipProjection = { kind: 'mesh' | 'standalone' | 'unknown'; meshId?: string; node?: ManagedFleetNodeReadModel; freshness: FleetControlFreshness }
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

function isRevisionRegression(
  candidate: ManagedFleetControlReadModel,
  current: ManagedFleetControlReadModel | undefined,
): boolean {
  if (
    !current
    || candidate.fleetId !== current.fleetId
    || candidate.controlNodeId !== current.controlNodeId
  ) return false
  return (
    candidate.revisions.topology < current.revisions.topology
    || candidate.revisions.trust < current.revisions.trust
    || candidate.revisions.accessPolicy < current.revisions.accessPolicy
  )
}

function isConverged(
  node: ManagedFleetControlReadModel['nodes'][number] | undefined,
): boolean {
  return Boolean(
    node &&
      !node.lastError &&
      node.desiredTopologyRevision === node.appliedTopologyRevision &&
      node.desiredTrustRevision === node.appliedTrustRevision &&
      node.desiredPolicyRevision === node.appliedPolicyRevision,
  )
}

export function Connections() {
  const { profiles, states, error, pair, retry, disconnect, client } = useConnectionRuntime()
  const { meshId: routeMeshId } = useParams()
  const { t, number } = useI18n()
  const [pairingLink, setPairingLink] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [controls, setControls] = useState<Record<string, ControlObservation>>(() => (
    Object.fromEntries(profiles.map((profile) => {
      const cached = loadCachedFleetControl(profile.instanceId)
      return [profile.instanceId, cached
        ? { control: cached.control, freshness: 'stale' as const, observedAt: cached.observedAt }
        : { freshness: 'unknown' as const }]
    }))
  ))
  const [meshName, setMeshName] = useState('Fleet')
  const [newMeshName, setNewMeshName] = useState('Fleet')
  const [newMeshControlInstanceId, setNewMeshControlInstanceId] = useState('')
  const [selectedMeshId, setSelectedMeshId] = useState('')
  const [controlBusy, setControlBusy] = useState(false)
  const [controlError, setControlError] = useState<string | null>(null)
  const [optimisticMeshes, setOptimisticMeshes] = useState<ManagedFleetMeshReadModel[] | null>(null)
  const [meshMutationPhase, setMeshMutationPhase] = useState<MutationPhase | null>(null)
  const [membershipMutations, setMembershipMutations] = useState<Record<string, MembershipMutation>>({})

  const refreshControls = useCallback(async () => {
    const results = await Promise.all(
      profiles.map(async (profile): Promise<readonly [string, ControlObservation]> => {
        const api = client(profile.instanceId)
        if (!api) {
          return [profile.instanceId, {
            freshness: 'unknown',
            error: 'control_client_unavailable',
          }]
        }
        try {
          const control = await api.fleetControl()
          return [profile.instanceId, {
            control,
            freshness: 'fresh',
            observedAt: Date.now(),
          }]
        } catch (cause) {
          const message = cause instanceof Error ? cause.message : 'control_unavailable'
          return [profile.instanceId, {
            freshness: 'unknown',
            error: message,
          }]
        }
      }),
    )

    for (const [instanceId, observation] of results) {
      if (!observation.control) continue
      const cached = loadCachedFleetControl(instanceId)?.control
      if (isRevisionRegression(observation.control, cached)) continue
      saveCachedFleetControl(instanceId, observation.control, observation.observedAt)
      propagateCachedFleetControl(observation.control, observation.observedAt)
    }

    setControls((current) => {
      const next = { ...current }
      for (const [instanceId, observation] of results) {
        const prior = next[instanceId]
        if (observation.control) {
          if (isRevisionRegression(observation.control, prior?.control)) {
            next[instanceId] = {
              ...prior,
              freshness: prior?.control ? 'stale' : 'unknown',
              error: 'control_revision_regression',
            }
          } else {
            next[instanceId] = observation
          }
        } else {
          next[instanceId] = {
            ...prior,
            freshness: prior?.control ? 'stale' : 'unknown',
            error: observation.error,
          }
        }
      }
      return next
    })
  }, [client, profiles])

  useEffect(() => {
    const handle = window.setTimeout(() => {
      void refreshControls()
    }, 0)
    return () => window.clearTimeout(handle)
  }, [refreshControls])

  const authorityViews = useMemo(() => {
    const grouped = new Map<string, ControlObservation & { control: ManagedFleetControlReadModel }>()
    const score = (item: ControlObservation & { control: ManagedFleetControlReadModel }) =>
      (item.control.nodeId === item.control.controlNodeId ? 100 : 0)
      + (item.freshness === 'fresh' ? 10 : item.freshness === 'stale' ? 1 : 0)
      + (item.observedAt ?? 0) / 1e15

    for (const profile of profiles) {
      const observation = controls[profile.instanceId]
      if (!observation?.control?.managed) continue
      const key = observation.control.fleetId + ':' + observation.control.controlNodeId
      const current = grouped.get(key)
      if (!current || score(observation as ControlObservation & { control: ManagedFleetControlReadModel }) > score(current)) {
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
      return { control, freshness: observation.freshness, observedAt: observation.observedAt, profile }
    })
  }, [controls, profiles, states])

  const authorityForMesh = useCallback((meshId: string | undefined): AuthorityView | undefined => (
    meshId ? authorityViews.find((view) => view.control.meshes.some((mesh) => mesh.meshId === meshId)) : undefined
  ), [authorityViews])

  const authorityForProfile = useCallback((profile: ConnectionProfile): AuthorityView | undefined => {
    const observed = controls[profile.instanceId]?.control
    if (observed?.managed) {
      const exact = authorityViews.find((view) => (
        view.control.fleetId === observed.fleetId
        && view.control.controlNodeId === observed.controlNodeId
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

  const confirmedStandalone = useCallback((profile: ConnectionProfile): boolean => {
    const observed = controls[profile.instanceId]
    if (!observed?.control) return false
    if (!observed.control.managed) return true
    const node = nodeForProfile(profile, observed.control, observed)
    return Boolean(node && !node.meshId)
  }, [controls])

  const eligibleControlProfiles = useMemo(() => profiles.filter((profile) => {
    if (states[profile.instanceId]?.status !== 'connected') return false
    const observed = controls[profile.instanceId]?.control
    if (!observed) return false
    return confirmedStandalone(profile) || (observed.managed && observed.nodeId === observed.controlNodeId)
  }), [confirmedStandalone, controls, profiles, states])

  useEffect(() => {
    const preferred = eligibleControlProfiles[0]?.instanceId ?? ''
    if (newMeshControlInstanceId && eligibleControlProfiles.some((profile) => profile.instanceId === newMeshControlInstanceId)) return
    const handle = window.setTimeout(() => setNewMeshControlInstanceId(preferred), 0)
    return () => window.clearTimeout(handle)
  }, [eligibleControlProfiles, newMeshControlInstanceId])

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
      setControlBusy(true)
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
          error: cause instanceof Error ? cause.message : 'control_mutation_failed',
        }
        setControlError(failure.error)
        return failure
      } finally {
        setControlBusy(false)
      }
    },
    [client, refreshControls],
  )

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitting(true)
    try {
      await pair(pairingLink, displayName || undefined)
      setPairingLink('')
      setDisplayName('')
    } catch {
      // Runtime exposes a safe user-visible error without credential material.
    } finally {
      setSubmitting(false)
    }
  }

  const membershipFor = useCallback((profile: ConnectionProfile): MembershipProjection => {
    const observed = controls[profile.instanceId]
    const mutation = membershipMutations[profile.instanceId]
    if (mutation && mutation.phase !== 'failed') {
      const targetAuthority = authorityForMesh(mutation.targetMeshId)
      return mutation.targetMeshId
        ? { kind: 'mesh', meshId: mutation.targetMeshId, freshness: targetAuthority?.freshness ?? observed?.freshness ?? 'unknown' }
        : { kind: 'standalone', freshness: observed?.freshness ?? 'unknown' }
    }
    if (observed?.control) {
      if (!observed.control.managed) {
        return { kind: 'standalone', freshness: observed.freshness }
      }
      const localNode = nodeForProfile(profile, observed.control, observed)
      if (localNode) {
        return localNode.meshId
          ? { kind: 'mesh', meshId: localNode.meshId, node: localNode, freshness: observed.freshness }
          : { kind: 'standalone', node: localNode, freshness: observed.freshness }
      }
    }
    for (const authority of authorityViews) {
      const node = nodeForProfile(profile, authority.control, observed)
      if (!node) continue
      return node.meshId
        ? { kind: 'mesh', meshId: node.meshId, node, freshness: authority.freshness }
        : { kind: 'standalone', node, freshness: authority.freshness }
    }
    return { kind: 'unknown', freshness: observed?.freshness ?? 'unknown' }
  }, [authorityForMesh, authorityViews, controls, membershipMutations])

  async function createMesh() {
    const displayName = newMeshName.trim() || 'Fleet'
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
    setControlBusy(true)
    setControlError(null)
    let result: ManagedFleetMutationResult
    try {
      const enrollment = await api.fleetEnrollment()
      result = await api.fleetControlMutation('/actions/fleet/control/adopt', {
        display_name: displayName,
        control_node_id: enrollment.nodeId,
      })
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
      result = { ok: false, error: cause instanceof Error ? cause.message : 'control_mutation_failed' }
      setControlError(result.error ?? 'control_mutation_failed')
    } finally {
      setControlBusy(false)
    }
    setOptimisticMeshes(null)
    setMeshMutationPhase(result.ok ? 'confirmed' : 'failed')
    if (result.ok) setNewMeshName('Fleet')
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
    setMeshMutationPhase(result.ok ? 'confirmed' : 'failed')
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
    setMeshMutationPhase(result.ok ? 'confirmed' : 'failed')
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
      const enrollment = await targetApi.fleetEnrollment()
      const result = await mutateControl(targetAuthority, '/actions/fleet/control/nodes/upsert', {
        node_id: enrollment.nodeId,
        mesh_id: meshId,
        origin: enrollment.origin,
        public_key: enrollment.publicKey,
        auth_token: enrollment.authToken,
        expected_topology_revision: targetAuthority.control.revisions.topology,
      })
      if (result.ok && result.control) {
        const node = result.control.nodes.find((item) => item.nodeId === enrollment.nodeId)
        const mesh = node?.meshId
          ? result.control.meshes.find((item) => item.meshId === node.meshId)
          : undefined
        const projected = { ...result.control, nodeId: enrollment.nodeId, mesh }
        const observedAt = Date.now()
        saveCachedFleetControl(instanceId, projected, observedAt)
        setControls((current) => ({
          ...current,
          [instanceId]: { control: projected, freshness: 'fresh', observedAt },
        }))
      }
      return result
    } catch (cause) {
      const failure = { ok: false, error: cause instanceof Error ? cause.message : 'enrollment_unavailable' }
      setControlError(failure.error)
      return failure
    }
    }, [authorityForMesh, client, mutateControl])
  async function detachFromMesh(instanceId: string): Promise<ManagedFleetMutationResult> {
    const profile = profiles.find((item) => item.instanceId === instanceId)
    if (!profile) return { ok: false, error: 'membership_unknown' }
    const sourceAuthority = authorityForProfile(profile)
    if (!sourceAuthority?.control.managed) return { ok: false, error: 'control_write_unavailable' }
    const node = nodeForProfile(profile, sourceAuthority.control, controls[instanceId])
    if (!node?.meshId) return { ok: false, error: 'membership_unknown' }
    return mutateControl(sourceAuthority, '/actions/fleet/control/nodes/detach', {
      node_id: node.nodeId,
      expected_topology_revision: sourceAuthority.control.revisions.topology,
    })
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
    const sameAuthority = (
      sourceAuthority.control.fleetId === targetAuthority.control.fleetId
      && sourceAuthority.control.controlNodeId === targetAuthority.control.controlNodeId
    )
    if (sameAuthority) {
      return mutateControl(sourceAuthority, '/actions/fleet/control/nodes/move', {
        node_id: node.nodeId,
        target_mesh_id: targetMeshId,
        expected_topology_revision: sourceAuthority.control.revisions.topology,
      })
    }
    const detached = await detachFromMesh(instanceId)
    if (!detached.ok) return detached
    return addToMesh(instanceId, targetMeshId)
  }

  async function changeMembership(instanceId: string, targetMeshId: string) {
    const profile = profiles.find((item) => item.instanceId === instanceId)
    if (!profile) return
    const current = membershipFor(profile)
    const currentMeshId = current.kind === 'mesh' ? current.meshId ?? '' : ''
    if (current.kind !== 'unknown' && currentMeshId === targetMeshId) return

    setMembershipMutations((value) => ({
      ...value,
      [instanceId]: { targetMeshId, phase: 'pending' },
    }))

    let result: ManagedFleetMutationResult
    if (!targetMeshId) {
      result = current.kind === 'mesh'
        ? await detachFromMesh(instanceId)
        : { ok: false, error: 'membership_unknown' }
    } else if (current.kind === 'mesh') {
      result = await moveToMesh(instanceId, targetMeshId)
    } else if (current.kind === 'standalone') {
      result = await addToMesh(instanceId, targetMeshId)
    } else {
      result = { ok: false, error: 'membership_unknown' }
    }

    setMembershipMutations((value) => ({
      ...value,
      [instanceId]: {
        targetMeshId,
        phase: result.ok ? 'confirmed' : 'failed',
        error: result.ok ? undefined : result.code ?? result.error ?? 'control_mutation_failed',
      },
    }))
  }

  async function rotateTrust(instanceId: string) {
    const observed = controls[instanceId]?.control
    const api = client(instanceId)
    if (!observed?.managed || !api) {
      setControlError('control_write_unavailable')
      return
    }
    setControlBusy(true)
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
      setControlError(cause instanceof Error ? cause.message : 'control_mutation_failed')
    } finally {
      setControlBusy(false)
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
    <section className="stack" aria-label={routeMeshId ? t('title.mesh') : t('connections.title')}>
      {!routeMeshId ? <div className="page-heading">
        <div>
          <p className="eyebrow">{t('connections.fleetAccess')}</p>
          <h2>{t('connections.title')}</h2>
          <p className="muted">{t('connections.restoreHint')}</p>
        </div>
        <span className="environment-badge">{number(profiles.length)} {t('connections.saved')}</span>
      </div> : null}

      {!routeMeshId ? <form className="panel connection-form" onSubmit={onSubmit}>
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
            onChange={(event) => setPairingLink(event.target.value)}
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
        <button type="submit" disabled={submitting}>
          {submitting ? t('connections.pairing') : t('connections.addServerAction')}
        </button>
        {error ? <p className="connection-error" role="alert">{error}</p> : null}
      </form> : null}

      {routeMeshId && !selectedMesh && meshes.length > 0 ? (
        <FeedbackState variant="partial" title={t('connections.unknown')} detail={t('connections.controlState') + ': ' + t('connections.unknown')} />
      ) : null}

      <article className="panel mesh-control">
        <div className="connection-card-heading">
          <div>
            <p className="eyebrow">{t('connections.mesh')}</p>
            <h3>{routeMeshId ? selectedMesh?.displayName ?? t('connections.unknown') : t('fleet.meshes')}</h3>
          </div>
          {routeMeshId && authoritative?.managed ? (
            <span className="status">{t('connections.controlNode')}: {authoritative.controlNodeId}</span>
          ) : null}
        </div>
        <p className="muted">{routeMeshId ? t('connections.manageHint') : t('fleet.meshesHint')}</p>
        {selectedMesh && routeMeshId ? (
          <div className="connection-actions">
            <Link className="nav-link" to={meshPersistentRoute(selectedMesh.meshId)}>{t('nav.slots')}</Link>
          </div>
        ) : null}
        {routeMeshId && authoritative?.managed ? (
          <div className="mesh-revisions">
            <span>{t('connections.topologyRevision')}: {number(authoritative.revisions.topology)}</span>
            <span>{t('connections.trustRevision')}: {number(authoritative.revisions.trust)}</span>
            <span>{t('connections.policyRevision')}: {number(authoritative.revisions.accessPolicy)}</span>
          </div>
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
            <div className="connection-actions">
              <button
                type="button"
                disabled={controlBusy || !writeProfile}
                onClick={() => void renameMesh()}
              >
                {t('connections.renameMesh')}
              </button>
              <button
                type="button"
                className="destructive-action"
                disabled={controlBusy || !writeProfile}
                onClick={() => void deleteMesh()}
              >
                {t('connections.deleteMesh')}
              </button>
            </div>
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
            disabled={controlBusy || eligibleControlProfiles.length === 0}
            onChange={(event) => setNewMeshControlInstanceId(event.target.value)}
          >
            {eligibleControlProfiles.map((profile) => (
              <option key={profile.instanceId} value={profile.instanceId}>{profile.displayName}</option>
            ))}
          </select>
        </label>
        <div className="connection-actions">
          <button
            type="button"
            disabled={controlBusy || !newMeshControlInstanceId}
            onClick={() => void createMesh()}
          >
            {t('connections.createMesh')}
          </button>
        </div>
        </> : null}
        {meshMutationPhase ? (
          <p className={meshMutationPhase === 'failed' ? 'connection-error' : 'muted'} role="status">
            {meshMutationPhase === 'pending'
              ? t('connections.pending')
              : meshMutationPhase === 'confirmed'
                ? t('connections.confirmed')
                : t('connections.failed')}
          </p>
        ) : null}
        {routeMeshId && authoritativeFreshness !== 'fresh' ? (
          <p className="muted" role="status">
            {t('connections.controlState')}: {authoritativeFreshness === 'stale' ? t('connections.stale') : t('connections.unknown')}
          </p>
        ) : null}
        {controlError ? <p className="connection-error" role="alert">{controlError}</p> : null}
      </article>

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
                  const membership = membershipFor(profile)
                  const member = membership.node
                  const memberMesh = membership.kind === 'mesh' && membership.meshId
                    ? meshes.find((mesh) => mesh.meshId === membership.meshId)
                    : undefined
                  const converged = isConverged(member)
                  const membershipMutation = membershipMutations[profile.instanceId]
                  const membershipLabel = membership.kind === 'mesh'
                    ? memberMesh?.displayName ?? membership.meshId ?? t('connections.unknown')
                    : membership.kind === 'standalone'
                      ? t('connections.standalone')
                      : t('connections.unknown')
                  return (
                    <article className="panel connection-card" key={profile.instanceId}>
                      <div className="connection-card-heading">
                        <div>
                          <h3>{profile.displayName}</h3>
                          <p className="muted">{profile.origin}</p>
                        </div>
                        <span className={'status connection-status-' + (state?.status ?? 'stored')}>
                          {statusLabel(state?.status, t)}
                        </span>
                      </div>
                      <p className="muted">{t('connections.device')}: {profile.metadata.deviceLabel}</p>
                      <div className="connection-fleet-status">
                        <span>
                          {t('connections.mesh')}: {membership.kind === 'mesh' && membership.meshId && !routeMeshId
                            ? <a className="text-link" href={meshRoute(membership.meshId)}>{membershipLabel}</a>
                            : membershipLabel}{membership.freshness === 'stale' ? ' · ' + t('connections.stale') : ''}
                        </span>
                        <span>{t('connections.reachability')}: {statusLabel(state?.status, t)}</span>
                        {member ? (
                          <span>
                            {t('connections.convergence')}: {converged ? t('connections.converged') : t('connections.pending')}
                          </span>
                        ) : null}
                      </div>
                      {routeMeshId && membership.kind !== 'unknown' && meshes.length > 0 ? (
                        <label className="mesh-membership-control ui-field">
                          <span>{t('connections.membership')}</span>
                          <select
                            value={membership.kind === 'mesh' ? membership.meshId ?? '' : ''}
                            disabled={controlBusy}
                            onChange={(event) => void changeMembership(
                              profile.instanceId,
                              event.target.value,
                            )}
                          >
                            <option value="">{t('connections.standalone')}</option>
                            {meshes.map((mesh) => (
                              <option key={mesh.meshId} value={mesh.meshId}>
                                {mesh.displayName}
                              </option>
                            ))}
                          </select>
                        </label>
                      ) : null}
                      {membershipMutation ? (
                        <p className={membershipMutation.phase === 'failed' ? 'connection-error' : 'muted'} role="status">
                          {membershipMutation.phase === 'pending'
                            ? t('connections.pending')
                            : membershipMutation.phase === 'confirmed'
                              ? t('connections.confirmed')
                              : t('connections.failed')}
                          {membershipMutation.error ? ': ' + membershipMutation.error : ''}
                        </p>
                      ) : null}
                      {member?.lastError ? <p className="connection-error">{member.lastError}</p> : null}
                      {observed?.error ? <p className="connection-error">{t('connections.controlError')}: {observed.error}</p> : null}
                      {state?.status === 'error' ? (
                        <p className="connection-error">
                          {state.message}{state.retryable ? ' — ' + t('connections.retryAvailable') : ''}
                        </p>
                      ) : null}
                      <div className="connection-actions">
                        {!routeMeshId && state?.status === 'error' && state.retryable ? (
                          <button type="button" onClick={() => void retry(profile.instanceId)}>
                            {t('connections.retry')}
                          </button>
                        ) : null}
                        {routeMeshId && observed?.control?.managed ? (
                          <button
                            type="button"
                            disabled={controlBusy || state?.status !== 'connected'}
                            onClick={() => void rotateTrust(profile.instanceId)}
                          >
                            {t('connections.rotateTrust')}
                          </button>
                        ) : null}
                        {!routeMeshId ? <button
                          type="button"
                          className="destructive-action"
                          onClick={() => disconnect(profile.instanceId)}
                        >
                          {t('connections.remove')}
                        </button> : null}
                      </div>
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
