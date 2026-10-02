import { type FormEvent, useCallback, useEffect, useMemo, useState } from 'react'

import type { ManagedFleetControlReadModel } from '../api/models'
import { useConnectionRuntime } from '../connections/runtime'
import type { MessageKey } from '../i18n/catalogs'
import { useI18n } from '../i18n/useI18n'
import { FeedbackState } from '../components/UiPrimitives'

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
  const { t, number } = useI18n()
  const [pairingLink, setPairingLink] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [controls, setControls] = useState<Record<string, ControlObservation>>({})
  const [meshName, setMeshName] = useState('Fleet')
  const [newMeshName, setNewMeshName] = useState('Fleet')
  const [selectedMeshId, setSelectedMeshId] = useState('')
  const [controlBusy, setControlBusy] = useState(false)
  const [controlError, setControlError] = useState<string | null>(null)

  const refreshControls = useCallback(async () => {
    const results = await Promise.all(
      profiles.map(async (profile) => {
        const api = client(profile.instanceId)
        if (!api) return [profile.instanceId, null] as const
        try {
          return [profile.instanceId, { control: await api.fleetControl() }] as const
        } catch (cause) {
          const message = cause instanceof Error ? cause.message : 'control_unavailable'
          return [profile.instanceId, { error: message }] as const
        }
      }),
    )
    setControls((current) => {
      const next = { ...current }
      for (const [instanceId, observation] of results) {
        if (observation === null) continue
        if ('control' in observation) {
          next[instanceId] = observation
        } else {
          next[instanceId] = {
            ...next[instanceId],
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

  const authoritative = useMemo(() => {
    const observed = profiles
      .map((profile) => controls[profile.instanceId]?.control)
      .filter((item): item is ManagedFleetControlReadModel => Boolean(item))
    return observed.find((item) => item.managed) ?? observed[0]
  }, [controls, profiles])

  const meshes = useMemo(() => authoritative?.meshes ?? [], [authoritative])

  const selectedMesh = useMemo(
    () => meshes.find((mesh) => mesh.meshId === selectedMeshId) ?? meshes[0],
    [meshes, selectedMeshId],
  )

  useEffect(() => {
    const handle = window.setTimeout(() => {
      if (!selectedMesh) {
        if (selectedMeshId) setSelectedMeshId('')
        return
      }
      if (selectedMeshId !== selectedMesh.meshId) {
        setSelectedMeshId(selectedMesh.meshId)
        setMeshName(selectedMesh.displayName)
      }
    }, 0)
    return () => window.clearTimeout(handle)
  }, [selectedMesh, selectedMeshId])

  const writeProfile = useMemo(() => {
    if (authoritative) {
      const controlProfile = profiles.find(
        (profile) =>
          states[profile.instanceId]?.status === 'connected' &&
          controls[profile.instanceId]?.control?.nodeId === authoritative.controlNodeId,
      )
      if (controlProfile) return controlProfile
    }
    return profiles.find((profile) => states[profile.instanceId]?.status === 'connected')
  }, [authoritative, controls, profiles, states])

  const mutateControl = useCallback(
    async (path: string, body: Record<string, unknown> = {}) => {
      if (!writeProfile) {
        setControlError('control_write_unavailable')
        return false
      }
      const api = client(writeProfile.instanceId)
      if (!api) {
        setControlError('control_write_unavailable')
        return false
      }
      setControlBusy(true)
      setControlError(null)
      try {
        const result = await api.fleetControlMutation(path, body)
        if (!result.ok) {
          setControlError(result.code ?? result.error ?? 'control_mutation_failed')
          return false
        }
        if (result.control) {
          setControls((current) => ({
            ...current,
            [writeProfile.instanceId]: { control: result.control },
          }))
        }
        await refreshControls()
        return true
      } catch (cause) {
        setControlError(cause instanceof Error ? cause.message : 'control_mutation_failed')
        return false
      } finally {
        setControlBusy(false)
      }
    },
    [client, refreshControls, writeProfile],
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

  async function createMesh() {
    const created = await mutateControl('/actions/fleet/control/adopt', {
      display_name: newMeshName.trim() || 'Fleet',
    })
    if (created) setNewMeshName('Fleet')
  }

  async function renameMesh() {
    if (!authoritative?.managed || !selectedMesh) return
    await mutateControl('/actions/fleet/control/mesh/rename', {
      mesh_id: selectedMesh.meshId,
      display_name: meshName.trim() || selectedMesh.displayName,
      expected_topology_revision: authoritative.revisions.topology,
    })
  }

  async function deleteMesh() {
    if (!authoritative?.managed || !selectedMesh) return
    await mutateControl('/actions/fleet/control/mesh/delete', {
      mesh_id: selectedMesh.meshId,
      expected_topology_revision: authoritative.revisions.topology,
    })
  }

  async function addToMesh(instanceId: string, meshId: string) {
    if (!authoritative?.managed) return
    const targetApi = client(instanceId)
    if (!targetApi) {
      setControlError('enrollment_unavailable')
      return
    }
    setControlError(null)
    try {
      const enrollment = await targetApi.fleetEnrollment()
      await mutateControl('/actions/fleet/control/nodes/upsert', {
        node_id: enrollment.nodeId,
        mesh_id: meshId,
        origin: enrollment.origin,
        public_key: enrollment.publicKey,
        auth_token: enrollment.authToken,
        expected_topology_revision: authoritative.revisions.topology,
      })
    } catch (cause) {
      setControlError(cause instanceof Error ? cause.message : 'enrollment_unavailable')
    }
  }

  async function detachFromMesh(instanceId: string) {
    if (!authoritative?.managed) return
    const observed = controls[instanceId]?.control
    if (!observed) return
    await mutateControl('/actions/fleet/control/nodes/detach', {
      node_id: observed.nodeId,
      expected_topology_revision: authoritative.revisions.topology,
    })
  }

  async function moveToMesh(instanceId: string, targetMeshId: string) {
    if (!authoritative?.managed) return
    const observed = controls[instanceId]?.control
    if (!observed) return
    await mutateControl('/actions/fleet/control/nodes/move', {
      node_id: observed.nodeId,
      target_mesh_id: targetMeshId,
      expected_topology_revision: authoritative.revisions.topology,
    })
  }

  async function changeMembership(instanceId: string, targetMeshId: string) {
    if (!authoritative) return
    const observed = controls[instanceId]?.control
    if (!observed) return
    const node = authoritative.nodes.find(
      (item) => item.nodeId === observed.nodeId && item.state !== 'detached',
    )
    const currentMeshId = node?.meshId ?? ''
    if (currentMeshId === targetMeshId) return
    if (!targetMeshId) {
      if (currentMeshId) await detachFromMesh(instanceId)
      return
    }
    if (currentMeshId) {
      await moveToMesh(instanceId, targetMeshId)
    } else {
      await addToMesh(instanceId, targetMeshId)
    }
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
        setControls((current) => ({
          ...current,
          [instanceId]: { control: result.control },
        }))
      }
      await refreshControls()
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
        const nodeId = controls[profile.instanceId]?.control?.nodeId
        const node = authoritative?.nodes.find(
          (item) => item.nodeId === nodeId && item.state !== 'detached',
        )
        return node?.meshId === mesh.meshId
      }),
    })),
    {
      key: 'standalone',
      label: t('connections.standalone'),
      profiles: profiles.filter((profile) => {
        const nodeId = controls[profile.instanceId]?.control?.nodeId
        const node = authoritative?.nodes.find(
          (item) => item.nodeId === nodeId && item.state !== 'detached',
        )
        return !node?.meshId
      }),
    },
  ].filter((group) => group.profiles.length > 0)

  return (
    <section className="stack" aria-labelledby="connections-title">
      <div className="page-heading">
        <div>
          <p className="eyebrow">{t('connections.fleetAccess')}</p>
          <h2 id="connections-title">{t('connections.title')}</h2>
          <p className="muted">{t('connections.restoreHint')}</p>
        </div>
        <span className="environment-badge">{number(profiles.length)} {t('connections.saved')}</span>
      </div>

      <form className="panel connection-form" onSubmit={onSubmit}>
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
      </form>

      <article className="panel mesh-control">
        <div className="connection-card-heading">
          <div>
            <p className="eyebrow">{t('connections.mesh')}</p>
            <h3>{selectedMesh?.displayName ?? t('connections.standalone')}</h3>
          </div>
          {authoritative?.managed ? (
            <span className="status">{t('connections.controlNode')}: {authoritative.controlNodeId}</span>
          ) : null}
        </div>
        <p className="muted">{t('connections.manageHint')}</p>
        {authoritative?.managed ? (
          <div className="mesh-revisions">
            <span>{t('connections.topologyRevision')}: {number(authoritative.revisions.topology)}</span>
            <span>{t('connections.trustRevision')}: {number(authoritative.revisions.trust)}</span>
            <span>{t('connections.policyRevision')}: {number(authoritative.revisions.accessPolicy)}</span>
          </div>
        ) : null}

        {meshes.length > 0 ? (
          <label className="ui-field">
            <span>{t('connections.mesh')}</span>
            <select
              value={selectedMesh?.meshId ?? ''}
              onChange={(event) => {
                const mesh = meshes.find((item) => item.meshId === event.target.value)
                if (!mesh) return
                setSelectedMeshId(mesh.meshId)
                setMeshName(mesh.displayName)
              }}
            >
              {meshes.map((mesh) => (
                <option key={mesh.meshId} value={mesh.meshId}>{mesh.displayName}</option>
              ))}
            </select>
          </label>
        ) : null}

        {selectedMesh ? (
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

        <label className="ui-field">
          <span>{t('connections.newMeshName')}</span>
          <input
            type="text"
            maxLength={120}
            value={newMeshName}
            onChange={(event) => setNewMeshName(event.target.value)}
          />
        </label>
        <div className="connection-actions">
          <button
            type="button"
            disabled={controlBusy || !writeProfile}
            onClick={() => void createMesh()}
          >
            {t('connections.createMesh')}
          </button>
        </div>
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
                  const nodeId = observed?.control?.nodeId
                  const member = authoritative?.managed && nodeId
                    ? authoritative.nodes.find(
                        (node) => node.nodeId === nodeId && node.state !== 'detached',
                      )
                    : undefined
                  const memberMesh = member?.meshId
                    ? meshes.find((mesh) => mesh.meshId === member.meshId)
                    : undefined
                  const converged = isConverged(member)
                  const sameFleet = Boolean(
                    authoritative
                    && observed?.control
                    && observed.control.fleetId === authoritative.fleetId,
                  )
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
                          {t('connections.mesh')}: {memberMesh?.displayName ?? t('connections.standalone')}
                        </span>
                        <span>{t('connections.reachability')}: {statusLabel(state?.status, t)}</span>
                        {member ? (
                          <span>
                            {t('connections.convergence')}: {converged ? t('connections.converged') : t('connections.pending')}
                          </span>
                        ) : null}
                      </div>
                      {authoritative?.managed && observed?.control && sameFleet ? (
                        <label className="mesh-membership-control ui-field">
                          <span>{t('connections.membership')}</span>
                          <select
                            value={member?.meshId ?? ''}
                            disabled={controlBusy || !writeProfile}
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
                      {member?.lastError ? <p className="connection-error">{member.lastError}</p> : null}
                      {observed?.error ? <p className="connection-error">{t('connections.controlError')}: {observed.error}</p> : null}
                      {state?.status === 'error' ? (
                        <p className="connection-error">
                          {state.message}{state.retryable ? ' — ' + t('connections.retryAvailable') : ''}
                        </p>
                      ) : null}
                      <div className="connection-actions">
                        {state?.status === 'error' && state.retryable ? (
                          <button type="button" onClick={() => void retry(profile.instanceId)}>
                            {t('connections.retry')}
                          </button>
                        ) : null}
                        {observed?.control?.managed && sameFleet ? (
                          <button
                            type="button"
                            disabled={controlBusy || state?.status !== 'connected'}
                            onClick={() => void rotateTrust(profile.instanceId)}
                          >
                            {t('connections.rotateTrust')}
                          </button>
                        ) : null}
                        <button
                          type="button"
                          className="destructive-action"
                          onClick={() => disconnect(profile.instanceId)}
                        >
                          {t('connections.remove')}
                        </button>
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
