import { useCallback, useEffect, useState } from 'react'

import type { ConsoleClient } from '../api/client'

type MeshVpnClient = Pick<ConsoleClient, 'meshVpnStatus' | 'meshVpnMutation'>
import type { MeshVpnStatus } from '../api/models'
import { useI18n } from '../i18n/useI18n'

type Peer = { instanceId: string; nodeId: string; displayName: string }

type Props = {
  instanceId: string
  nodeId: string
  peers: Peer[]
  client: (instanceId: string) => MeshVpnClient | null | undefined
  expectedTopologyRevision?: number
}

export function MeshVpnPanel({ instanceId, nodeId, peers, client, expectedTopologyRevision }: Props) {
  const { t } = useI18n()
  const [expanded, setExpanded] = useState(false)
  const [view, setView] = useState<MeshVpnStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<string | null>(null)
  const [backend, setBackend] = useState<'auto' | 'kernel' | 'userspace'>('auto')
  const [overlayIp, setOverlayIp] = useState('')
  const [endpoint, setEndpoint] = useState('')
  const [listenPort, setListenPort] = useState(53148)
  const [peerId, setPeerId] = useState('')

  const refresh = useCallback(async () => {
    const api = client(instanceId)
    if (!api) throw new Error('connection_unavailable')
    const status = await api.meshVpnStatus()
    setView(status)
    if (status.backend) setBackend(status.backend)
    if (status.overlay_ip) setOverlayIp(status.overlay_ip)
    if (status.endpoint) setEndpoint(status.endpoint)
  }, [instanceId, client])

  useEffect(() => {
    if (!expanded) return
    void refresh().catch((cause: unknown) => setError(cause instanceof Error ? cause.message : 'vpn_unavailable'))
  }, [expanded, refresh])

  async function perform(action: 'prepare' | 'activate' | 'backend' | 'switch' | 'revoke',
                         values: Record<string, unknown>) {
    const api = client(instanceId)
    if (!api) return
    setBusy(true)
    setError(null)
    setFeedback(null)
    try {
      const result = await api.meshVpnMutation(action, values)
      if (!result.ok) throw new Error(result.error ?? result.code ?? 'vpn_unavailable')
      setFeedback(result.restart_required ? 'restart_required' : 'confirmed')
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'vpn_unavailable')
    } finally {
      setBusy(false)
    }
  }

  async function exchange() {
    const own = client(instanceId)
    const selected = peers.find((item) => item.instanceId === peerId)
    const other = selected ? client(selected.instanceId) : null
    if (!own || !other) return
    setBusy(true)
    setError(null)
    setFeedback(null)
    try {
      // Every exchange uses fresh signed manifests; the backend verifies the
      // already pinned, managed Fleet public key for *both* issuer directions.
      const [a, b] = await Promise.all([own.meshVpnStatus(), other.meshVpnStatus()])
      if (!a.prepared || !b.prepared || !a.offer || !b.offer) {
        throw new Error(t('vpn.notReady'))
      }
      if (!selected) throw new Error('unknown_peer')
      const forward = await own.meshVpnMutation('enroll', {
        peer_id: selected.nodeId, offer: b.offer,
      })
      if (!forward.ok) throw new Error(forward.error ?? 'vpn_enrollment_failed')
      const reverse = await other.meshVpnMutation('enroll', {
        peer_id: nodeId, offer: a.offer,
      })
      if (!reverse.ok) throw new Error(reverse.error ?? 'vpn_enrollment_failed')
      setFeedback('confirmed')
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'vpn_unavailable')
    } finally {
      setBusy(false)
    }
  }

  const chosen = peers.find((item) => item.instanceId === peerId)
  const peer = view?.peers.find((item) => item.node_id === chosen?.nodeId)
  return (
    <section className="ui-field mesh-vpn-panel" aria-label={t('vpn.title')}>
      <button type="button" className="chip" aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}>{t('vpn.title')}</button>
      {expanded ? (
        <div className="stack">
          <div className="connection-fleet-status">
            <span>{view?.tunnel?.running ? t('vpn.ready') : t('vpn.offline')}</span>
            <span>{t('vpn.backend')}: {view?.backend ?? '—'}</span>
          </div>
          {!view?.prepared ? (
            <div className="stack">
              <label className="ui-field">{t('vpn.overlay')}
                <input type="text" value={overlayIp} onChange={(e) => setOverlayIp(e.target.value)}
                  placeholder="10.244.12.1" autoComplete="off" />
              </label>
              <label className="ui-field">{t('vpn.endpoint')}
                <input type="text" value={endpoint} onChange={(e) => {
                  const value = e.target.value
                  setEndpoint(value)
                  const port = Number(value.split(':').at(-1))
                  if (Number.isInteger(port) && port > 0 && port <= 65535) setListenPort(port)
                }}
                  placeholder="203.0.113.10:53148" autoComplete="off" />
              </label>
              <label className="ui-field">{t('vpn.listenPort')}
                <input type="number" min={1} max={65535} value={listenPort}
                  onChange={(e) => setListenPort(Number(e.target.value))} />
              </label>
              <label className="ui-field">{t('vpn.backend')}
                <select value={backend} onChange={(e) => setBackend(e.target.value as typeof backend)}>
                  <option value="auto">auto</option>
                  <option value="kernel">kernel</option>
                  <option value="userspace">userspace</option>
                </select>
              </label>
              <button type="button" className="chip" disabled={busy || !overlayIp || !endpoint || listenPort < 1 || listenPort > 65535}
                onClick={() => void perform('prepare', {
                  backend, overlay_ip: overlayIp, endpoint, listen_port: listenPort,
                })}>
                {t('vpn.prepare')}
              </button>
            </div>
          ) : (
            <div className="stack">
              <label className="ui-field">{t('vpn.backend')}
                <select value={backend} disabled={busy || Boolean(view.tunnel?.running)}
                  onChange={(e) => setBackend(e.target.value as typeof backend)}>
                  <option value="auto">auto</option>
                  <option value="kernel">kernel</option>
                  <option value="userspace">userspace</option>
                </select>
              </label>
              <button type="button" className="chip"
                disabled={busy || Boolean(view.tunnel?.running) || backend === view.backend}
                onClick={() => void perform('backend', { backend })}>{t('vpn.backend')}</button>
              <label className="ui-field">{t('vpn.peer')}
                <select value={peerId} onChange={(e) => setPeerId(e.target.value)}>
                  <option value="">—</option>
                  {peers.map((item) => (
                    <option key={item.instanceId} value={item.instanceId}>{item.displayName}</option>
                  ))}
                </select>
              </label>
              <div className="membership-target-options">
                <button type="button" className="chip" disabled={busy || !peerId}
                  onClick={() => void exchange()}>{t('vpn.exchange')}</button>
                <button type="button" className="chip" disabled={busy || !view.peers.some((p) => p.enrolled)}
                  onClick={() => void perform('activate', {})}>{t('vpn.start')}</button>
                <button type="button" className="chip" disabled={busy || !peerId || !peer?.enrolled}
                  onClick={() => void perform('switch', {
                    peer_id: chosen?.nodeId,
                    mode: peer?.mode === 'wireguard' ? 'https' : 'wireguard',
                    expected_topology_revision: expectedTopologyRevision,
                  })}>
                  {peer?.mode === 'wireguard' ? t('vpn.rollback') : t('vpn.enable')}
                </button>
                <button type="button" className="chip" disabled={busy || !peer?.enrolled}
                  onClick={() => void perform('revoke', { peer_id: chosen?.nodeId })}>{t('vpn.revoke')}</button>
              </div>
            </div>
          )}
          <button type="button" className="chip" disabled={busy}
            onClick={() => void refresh().catch((cause: unknown) => setError(
              cause instanceof Error ? cause.message : 'vpn_unavailable',
            ))}>{t('vpn.refresh')}</button>
          <p className="muted" role={error ? 'alert' : 'status'} aria-live="polite">
            {error || feedback || '\u00a0'}
          </p>
        </div>
      ) : null}
    </section>
  )
}
