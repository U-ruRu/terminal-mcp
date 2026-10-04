import { type FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import type { ManagedFleetControlReadModel, ManagedFleetMeshReadModel, ManagedFleetMutationResult, ManagedFleetNodeReadModel } from '../api/models'
import { useConnectionRuntime } from '../connections/runtime'
import { isFleetControlRevisionRegression, loadCachedFleetControl, propagateCachedFleetControl, saveCachedFleetControl, type FleetControlFreshness } from '../connections/controlState'
import type { ConnectionProfile } from '../connections/types'
import type { MessageKey } from '../i18n/catalogs'
import { useI18n } from '../i18n/useI18n'
import { ConfirmationDialog, FeedbackState, IconButton, IconButtonRow } from '../components/UiPrimitives'
import { returnToState } from '../navigation/context'
import { meshPersistentRoute, meshRoute, serverRoute } from '../navigation/routes'

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
type OperationalState = 'current' | 'applying' | 'unavailable' | 'attention'
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

function operationalLabel(state: OperationalState, t: (key: MessageKey) => string): string {
  if (state === 'current') return t('connections.current')
  if (state === 'applying') return t('connections.applying')
  if (state === 'attention') return t('connections.actionRequired')
  return t('connections.unavailable')
}

function resolvedMembershipMutation(
  membership: MembershipProjection,
  mutation: MembershipMutation | undefined,
): MembershipMutation | undefined {
  if (!mutation || mutation.phase !== 'pending' || membership.freshness !== 'fresh') return mutation
  const confirmed = mutation.targetMeshId
    ? membership.kind === 'mesh' && membership.meshId === mutation.targetMeshId
    : membership.kind === 'standalone'
  return confirmed ? { ...mutation, phase: 'confirmed' } : mutation
}

function memberOperationalState(
  membership: MembershipProjection,
  mutation: MembershipMutation | undefined,
): OperationalState {
  if (mutation?.phase === 'failed') return 'attention'
  if (mutation?.phase === 'pending') return 'applying'
  if (membership.freshness !== 'fresh' || membership.kind === 'unknown') return 'unavailable'
  if (membership.node?.lastError) return 'attention'
  if (membership.kind === 'mesh' && !isConverged(membership.node)) return 'applying'
  return 'current'
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
  const [membershipTargets, setMembershipTargets] = useState<Record<string, string>>({})
  const [confirmation, setConfirmation] = useState<{ kind: 'delete-mesh'; label: string } | { kind: 'remove-connection'; instanceId: string; label: string } | null>(null)

  const refreshControls = useCallback(async () => {
    const results = await Promise.all(
      profiles.map(async (profile): Promise<readonly [string, ControlObservation]> => {
        if (states[profile.instanceId]?.status !== 'connected') {
          return [profile.instanceId, {
            freshness: 'unknown',
            error: 'control_auth_unavailable',
          }]
        }
        try {
          const api = client(profile.instanceId)
          if (!api) {
            return [profile.instanceId, {
              freshness: 'unknown',
              error: 'control_client_unavailable',
            }]
          }
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
      if (isFleetControlRevisionRegression(observation.control, cached)) continue
      saveCachedFleetControl(instanceId, observation.control, observation.observedAt)
      propagateCachedFleetControl(observation.control, observation.observedAt)
    }

    setControls((current) => {
      const next = { ...current }
      for (const [instanceId, observation] of results) {
        const prior = next[instanceId]
        if (observation.control) {
          if (isFleetControlRevisionRegression(observation.control, prior?.control)) {
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
  }, [client, profiles, states])

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
  const selectedMembers = useMemo(() => (
    selectedMesh && authoritative
      ? authoritative.nodes.filter((node) => node.meshId === selectedMesh.meshId && node.state !== 'detached')
      : []
  ), [authoritative, selectedMesh])
  const meshOperationalState: OperationalState = (() => {
    if (meshMutationPhase === 'failed') return 'attention'
    if (meshMutationPhase === 'pending') return 'applying'
    if (!selectedMesh || !authoritative || authoritativeFreshness !== 'fresh') return 'unavailable'
    if (controlError || selectedMembers.some((node) => Boolean(node.lastError))) return 'attention'
    if (selectedMembers.some((node) => !isConverged(node))) return 'applying'
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

  const confirmedStandalone = useCallback((profile: ConnectionProfile): boolean => {
    const observed = controls[profile.instanceId]
    if (observed?.control && !observed.control.managed) return true
    const authority = authorityForProfile(profile)
    const control = observed?.control?.managed ? observed.control : authority?.control
    if (!control) return false
    const node = nodeForProfile(profile, control, observed)
    return Boolean(node && node.state !== 'detached' && !node.meshId)
  }, [authorityForProfile, controls])

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
    const authorityCandidates = authorityViews
      .map((authority) => ({ authority, node: nodeForProfile(profile, authority.control, observed) }))
      .filter((item): item is typeof item & { node: ManagedFleetNodeReadModel } => Boolean(item.node))
      .sort((left, right) => {
        const a = left.authority.control
        const b = right.authority.control
        if (a.fleetId === b.fleetId && a.controlNodeId === b.controlNodeId) {
          const topology = b.revisions.topology - a.revisions.topology
          if (topology !== 0) return topology
        }
        return (right.authority.observedAt ?? 0) - (left.authority.observedAt ?? 0)
      })
    const authoritative = authorityCandidates[0]

    if (authoritative) {
      const candidate = authoritative.authority
      const observedControl = observed?.control
      const sameAuthority = Boolean(
        observedControl
        && observedControl.fleetId === candidate.control.fleetId
        && observedControl.controlNodeId === candidate.control.controlNodeId
      )
      const freshnessRank = (value: FleetControlFreshness) => value === 'fresh' ? 2 : value === 'stale' ? 1 : 0
      const supersedesObserved = !observedControl
        || (
          sameAuthority
          && (
            candidate.control.revisions.topology > observedControl.revisions.topology
            || (
              candidate.control.revisions.topology === observedControl.revisions.topology
              && (
                freshnessRank(candidate.freshness) > freshnessRank(observed?.freshness ?? 'unknown')
                || (
                  freshnessRank(candidate.freshness) === freshnessRank(observed?.freshness ?? 'unknown')
                  && (candidate.observedAt ?? 0) > (observed?.observedAt ?? 0)
                )
              )
            )
          )
        )
        || (!sameAuthority && (candidate.observedAt ?? 0) > (observed?.observedAt ?? 0))

      if (supersedesObserved) {
        return authoritative.node.meshId
          ? { kind: 'mesh', meshId: authoritative.node.meshId, node: authoritative.node, freshness: candidate.freshness }
          : { kind: 'standalone', node: authoritative.node, freshness: candidate.freshness }
      }
    }

    if (observed?.control) {
      if (!observed.control.managed) {
        return { kind: 'standalone', freshness: observed.freshness }
      }
      const observedNode = observed.control.nodes.find((node) => node.nodeId === observed.control?.nodeId)
        ?? observed.control.nodes.find((node) => normalizedOrigin(node.origin) === normalizedOrigin(profile.origin))
      if (observedNode?.state === 'detached') {
        return { kind: 'standalone', node: observedNode, freshness: observed.freshness }
      }
      const localNode = nodeForProfile(profile, observed.control, observed)
      if (localNode) {
        return localNode.meshId
          ? { kind: 'mesh', meshId: localNode.meshId, node: localNode, freshness: observed.freshness }
          : { kind: 'standalone', node: localNode, freshness: observed.freshness }
      }
    }

    if (authoritative) {
      return authoritative.node.meshId
        ? { kind: 'mesh', meshId: authoritative.node.meshId, node: authoritative.node, freshness: authoritative.authority.freshness }
        : { kind: 'standalone', node: authoritative.node, freshness: authoritative.authority.freshness }
    }
    return { kind: 'unknown', freshness: observed?.freshness ?? 'unknown' }
  }, [authorityViews, controls])

  const projectMembershipControl = useCallback((
    instanceId: string,
    control: ManagedFleetControlReadModel,
    nodeId: string,
  ) => {
    const node = control.nodes.find((item) => item.nodeId === nodeId)
    const mesh = node?.meshId ? control.meshes.find((item) => item.meshId === node.meshId) : undefined
    const projected = { ...control, nodeId, mesh }
    const observedAt = Date.now()
    saveCachedFleetControl(instanceId, projected, observedAt)
    setControls((current) => ({
      ...current,
      [instanceId]: { control: projected, freshness: 'fresh', observedAt },
    }))
  }, [])

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
    setMeshMutationPhase(result.ok && result.control?.meshes.some((mesh) => mesh.displayName === displayName) ? 'confirmed' : result.ok ? 'pending' : 'failed')
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
    const profile = profiles.find((item) => item.instanceId === instanceId)
    const knownNode = profile
      ? nodeForProfile(profile, targetAuthority.control, controls[instanceId])
      : undefined
    if (knownNode && knownNode.state !== 'detached') {
      const result = await mutateControl(targetAuthority, '/actions/fleet/control/nodes/move', {
        node_id: knownNode.nodeId,
        target_mesh_id: meshId,
        expected_topology_revision: targetAuthority.control.revisions.topology,
      })
      if (result.ok && result.control) {
        projectMembershipControl(instanceId, result.control, knownNode.nodeId)
      }
      return result
    }

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
        projectMembershipControl(instanceId, result.control, enrollment.nodeId)
      }
      return result
    } catch (cause) {
      const failure = { ok: false, error: cause instanceof Error ? cause.message : 'enrollment_unavailable' }
      setControlError(failure.error)
      return failure
    }
    }, [authorityForMesh, client, controls, mutateControl, profiles, projectMembershipControl])
  async function detachFromMesh(instanceId: string): Promise<ManagedFleetMutationResult> {
    const profile = profiles.find((item) => item.instanceId === instanceId)
    if (!profile) return { ok: false, error: 'membership_unknown' }
    const sourceAuthority = authorityForProfile(profile)
    if (!sourceAuthority?.control.managed) return { ok: false, error: 'control_write_unavailable' }
    const node = nodeForProfile(profile, sourceAuthority.control, controls[instanceId])
    if (!node?.meshId) return { ok: false, error: 'membership_unknown' }
    const result = await mutateControl(sourceAuthority, '/actions/fleet/control/nodes/detach', {
      node_id: node.nodeId,
      expected_topology_revision: sourceAuthority.control.revisions.topology,
    })
    if (result.ok && result.control) projectMembershipControl(instanceId, result.control, node.nodeId)
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
    const sameAuthority = (
      sourceAuthority.control.fleetId === targetAuthority.control.fleetId
      && sourceAuthority.control.controlNodeId === targetAuthority.control.controlNodeId
    )
    if (sameAuthority) {
      const result = await mutateControl(sourceAuthority, '/actions/fleet/control/nodes/move', {
        node_id: node.nodeId,
        target_mesh_id: targetMeshId,
        expected_topology_revision: sourceAuthority.control.revisions.topology,
      })
      if (result.ok && result.control) projectMembershipControl(instanceId, result.control, node.nodeId)
      return result
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
    const alreadyAuthoritative = (
      current.kind !== 'unknown'
      && currentMeshId === targetMeshId
      && current.freshness === 'fresh'
      && (current.kind === 'standalone' || isConverged(current.node))
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

    let result: ManagedFleetMutationResult
    if (!targetMeshId) {
      result = current.kind === 'mesh'
        ? await detachFromMesh(instanceId)
        : { ok: false, error: 'membership_unknown' }
    } else if (current.kind === 'mesh' && currentMeshId === targetMeshId) {
      // Re-apply the displayed target through authority. This is intentionally not
      // a UI no-op: a stale replica may still display the desired Mesh after detach.
      result = await addToMesh(instanceId, targetMeshId)
    } else if (current.kind === 'mesh') {
      result = await moveToMesh(instanceId, targetMeshId)
    } else if (current.kind === 'standalone') {
      result = await addToMesh(instanceId, targetMeshId)
    } else {
      result = { ok: false, error: 'membership_unknown' }
    }

    const authoritativeNode = result.control?.nodes.find((node) => (
      current.node?.nodeId ? node.nodeId === current.node.nodeId : normalizedOrigin(node.origin) === normalizedOrigin(profile.origin)
    ))
    const confirmed = Boolean(result.ok && authoritativeNode && (
      targetMeshId
        ? authoritativeNode.state !== 'detached' && authoritativeNode.meshId === targetMeshId
        : authoritativeNode.state === 'detached' || !authoritativeNode.meshId
    ))
    setMembershipMutations((value) => ({
      ...value,
      [instanceId]: {
        targetMeshId,
        phase: result.ok ? (confirmed ? 'confirmed' : 'pending') : 'failed',
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
        <IconButton type="submit" icon="connect" variant="primary" label={t('connections.addServerAction')} busy={submitting} />
        <p className={'mutation-status-slot ' + (error ? 'connection-error' : 'muted')} role={error ? 'alert' : undefined} aria-live={error ? 'polite' : undefined}>{error || '\u00a0'}</p>
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
            <Link className="nav-link" to={meshPersistentRoute(selectedMesh.meshId)}>{t('nav.slots')}</Link>
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
            <IconButtonRow className="connection-actions">
              <IconButton icon="edit" variant="secondary" label={t('connections.renameMesh')} disabled={controlBusy || !writeProfile} onClick={() => void renameMesh()} />
              <IconButton icon="delete" variant="destructive" label={t('connections.deleteMesh')} disabled={controlBusy || !writeProfile} onClick={() => { if (selectedMesh) setConfirmation({ kind: 'delete-mesh', label: selectedMesh.displayName }) }} />
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
            disabled={controlBusy || eligibleControlProfiles.length === 0}
            onChange={(event) => setNewMeshControlInstanceId(event.target.value)}
          >
            {eligibleControlProfiles.map((profile) => (
              <option key={profile.instanceId} value={profile.instanceId}>{profile.displayName}</option>
            ))}
          </select>
        </label>
        <div className="connection-actions">
          <IconButton icon="create" variant="primary" label={t('connections.createMesh')} disabled={controlBusy || !newMeshControlInstanceId} onClick={() => void createMesh()} />
        </div>
        </> : null}
        <p className={'mutation-status-slot ' + (controlError || meshMutationPhase === 'failed' ? 'connection-error' : 'muted')} role={controlError ? 'alert' : meshMutationPhase || (routeMeshId && authoritativeFreshness !== 'fresh') ? 'status' : undefined} aria-live={controlError || meshMutationPhase || (routeMeshId && authoritativeFreshness !== 'fresh') ? 'polite' : undefined}>
          {controlError
            ? t('connections.actionRequired')
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
        busy={confirmation.kind === 'delete-mesh' && controlBusy}
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
                  const membership = membershipFor(profile)
                  const member = membership.node
                  const memberMesh = membership.kind === 'mesh' && membership.meshId
                    ? meshes.find((mesh) => mesh.meshId === membership.meshId)
                    : undefined
                  const membershipMutation = resolvedMembershipMutation(
                    membership,
                    membershipMutations[profile.instanceId],
                  )
                  const memberState = memberOperationalState(membership, membershipMutation)
                  const membershipLabel = membership.kind === 'mesh'
                    ? memberMesh?.displayName ?? membership.meshId ?? t('connections.unknown')
                    : membership.kind === 'standalone'
                      ? t('connections.standalone')
                      : t('connections.unknown')
                  const authoritativeTarget = membership.kind === 'mesh' ? membership.meshId ?? '' : ''
                  const membershipTarget = membershipTargets[profile.instanceId] ?? authoritativeTarget
                  return (
                    <article className="panel connection-card" key={profile.instanceId}>
                      <div className="connection-card-heading">
                        <div>
                          <h3>{routeMeshId ? <Link className="text-link" to={serverRoute(profile.instanceId)} state={returnToState(meshRoute(routeMeshId))}>{profile.displayName}</Link> : profile.displayName}</h3>
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
                        {membership.kind === 'mesh' || membershipMutation ? (
                          <span>
                            {t('connections.syncState')}: {operationalLabel(memberState, t)}
                          </span>
                        ) : null}
                      </div>
                      {routeMeshId && membership.kind !== 'unknown' && meshes.length > 0 ? (
                        <div className="mesh-membership-control ui-field">
                          <span>{t('connections.authoritativeMembership')}: {membershipLabel}</span>
                          <span>{t('connections.targetMembership')}</span>
                          <div className="membership-target-options" role="group" aria-label={t('connections.targetMembership')}>
                            <button
                              type="button"
                              className={membershipTarget === '' ? 'chip active' : 'chip'}
                              aria-pressed={membershipTarget === ''}
                              disabled={controlBusy || membershipMutation?.phase === 'pending'}
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
                                disabled={controlBusy || membershipMutation?.phase === 'pending'}
                                onClick={() => setMembershipTargets((value) => ({ ...value, [profile.instanceId]: mesh.meshId }))}
                              >
                                {mesh.displayName}
                              </button>
                            ))}
                          </div>
                          <IconButton className="membership-commit" icon="apply" variant="primary" label={t('connections.applyMembership')} busy={membershipMutation?.phase === 'pending'} disabled={controlBusy} onClick={() => void changeMembership(profile.instanceId, membershipTarget)} />
                        </div>
                      ) : null}
                      <p className={'mutation-status-slot connection-card-status-slot ' + (membershipMutation?.phase === 'failed' || member?.lastError || observed?.error || state?.status === 'error' ? 'connection-error' : 'muted')} role={member?.lastError || observed?.error || state?.status === 'error' ? 'alert' : membershipMutation ? 'status' : undefined} aria-live={member?.lastError || observed?.error || state?.status === 'error' || membershipMutation ? 'polite' : undefined}>
                        {state?.status === 'error'
                          ? t('connections.actionRequired') + (state.retryable ? ' · ' + t('connections.retryAvailable') : '')
                          : observed?.error
                            ? t('connections.controlError')
                            : member?.lastError
                              ? t('connections.actionRequired')
                              : membershipMutation?.phase === 'pending'
                                ? t('connections.applying')
                                : membershipMutation?.phase === 'confirmed'
                                  ? t('connections.confirmed')
                                  : membershipMutation?.phase === 'failed'
                                    ? t('connections.actionRequired')
                                    : '\u00a0'}
                      </p>
                      <IconButtonRow className="connection-actions">
                        {!routeMeshId && state?.status === 'error' && state.retryable ? (
                          <IconButton icon="retry" variant="primary" label={t('connections.retry')} onClick={() => void retry(profile.instanceId)} />
                        ) : null}
                        {routeMeshId && observed?.control?.managed ? (
                          <IconButton icon="rotate" variant="secondary" label={t('connections.rotateTrust')} disabled={controlBusy || state?.status !== 'connected'} onClick={() => void rotateTrust(profile.instanceId)} />
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
