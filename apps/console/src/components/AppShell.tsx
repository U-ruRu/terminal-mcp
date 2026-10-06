import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'

import { navigationReturnTo, serverSwitchDestination } from '../navigation/context'
import { APK_VERSION_LABEL } from '../appVersion'

import type { FleetServerReadModel } from '../fleet/readModel'
import type { MessageKey } from '../i18n/catalogs'
import { useI18n } from '../i18n/useI18n'
import { Icon, type IconName } from './Icon'
import { IconButton } from './UiPrimitives'
import { StatusBadge } from './StatusBadge'
import { needsAttention, serverVisualState, type ServerVisualState } from './serverPresentation'

type NavigationKey = 'fleet' | 'overview' | 'connections' | 'agents' | 'slots' | 'tasks' | 'activity' | 'context' | 'health' | 'settings'

type NavigationItem = {
  key: NavigationKey
  labelKey: MessageKey
  globalPath: string
  icon: IconName
  serverPath?: (instanceId: string) => string
  meshPath?: (meshId: string) => string
}

type ResolvedNavigationItem = Pick<NavigationItem, 'key' | 'labelKey' | 'icon'> & { to: string; active: boolean }

const globalNavigation: NavigationItem[] = [
  { key: 'fleet', labelKey: 'nav.fleet', globalPath: '/', icon: 'fleet' },
  { key: 'slots', labelKey: 'nav.slots', globalPath: '/slots', icon: 'slots' },
  { key: 'connections', labelKey: 'nav.connections', globalPath: '/connections', icon: 'connections' },
  { key: 'settings', labelKey: 'nav.settings', globalPath: '/settings', icon: 'settings' },
]

const serverNavigation: NavigationItem[] = [
  { key: 'agents', icon: 'agents', labelKey: 'nav.agents', globalPath: '/agents', serverPath: (instanceId) => '/servers/' + encodeURIComponent(instanceId) + '/agents' },
  { key: 'slots', icon: 'slots', labelKey: 'nav.slots', globalPath: '/slots', serverPath: (instanceId) => '/servers/' + encodeURIComponent(instanceId) + '/slots' },
  { key: 'tasks', icon: 'tasks', labelKey: 'nav.tasks', globalPath: '/tasks', serverPath: (instanceId) => '/servers/' + encodeURIComponent(instanceId) + '/tasks' },
  { key: 'activity', icon: 'activity', labelKey: 'nav.activity', globalPath: '/activity', serverPath: (instanceId) => '/activity?server=' + encodeURIComponent(instanceId) },
  { key: 'context', icon: 'context', labelKey: 'nav.context', globalPath: '/context', serverPath: (instanceId) => '/servers/' + encodeURIComponent(instanceId) + '/context' },
  { key: 'health', icon: 'health', labelKey: 'nav.health', globalPath: '/health', serverPath: (instanceId) => '/servers/' + encodeURIComponent(instanceId) + '/health' },
]

const meshNavigation: NavigationItem[] = [
  { key: 'agents', icon: 'agents', labelKey: 'nav.agents', globalPath: '/agents', meshPath: (meshId) => '/meshes/' + encodeURIComponent(meshId) + '/agents' },
  { key: 'slots', icon: 'slots', labelKey: 'nav.slots', globalPath: '/slots', meshPath: (meshId) => '/meshes/' + encodeURIComponent(meshId) + '/persistent' },
  { key: 'tasks', icon: 'tasks', labelKey: 'nav.tasks', globalPath: '/tasks', meshPath: (meshId) => '/meshes/' + encodeURIComponent(meshId) + '/tasks' },
  { key: 'activity', icon: 'activity', labelKey: 'nav.activity', globalPath: '/activity', meshPath: (meshId) => '/meshes/' + encodeURIComponent(meshId) + '/activity' },
]

function serverStatusLabel(state: ServerVisualState, t: (key: MessageKey) => string): string {
  if (state === 'healthy') return t('status.live')
  if (state === 'stale') return t('status.stale')
  if (state === 'offline') return t('status.offline')
  if (state === 'loading') return t('status.catchingUp')
  return t('fleet.needsAttention')
}

function pathServer(pathname: string): string | undefined {
  const match = /^\/servers\/([^/]+)/.exec(pathname)
  if (!match) return undefined
  try {
    return decodeURIComponent(match[1])
  } catch {
    return undefined
  }
}

function activeKey(pathname: string): NavigationKey {
  if (pathname === '/') return 'fleet'
  if (pathname === '/activity' || pathname.endsWith('/activity')) return 'activity'
  if (pathname === '/settings') return 'settings'
  if (pathname === '/connections' || pathname === '/connect') return 'connections'
  if (pathname.startsWith('/meshes/') && pathname.includes('/persistent')) return 'slots'
  if (pathname === '/agents' || pathname.endsWith('/agents')) return 'agents'
  if (pathname === '/slots' || pathname.includes('/slots')) return 'slots'
  if (pathname === '/tasks' || pathname.includes('/tasks')) return 'tasks'
  if (pathname === '/context' || pathname.endsWith('/context')) return 'context'
  if (pathname === '/health' || pathname.endsWith('/health')) return 'health'
  if (/^\/servers\/[^/]+\/?$/.test(pathname)) return 'fleet'
  return 'fleet'
}

type ContextNavigation = {
  to: string
  ariaKey: MessageKey
  titleKey: MessageKey
}

function contextNavigation(pathname: string, search: string, returnTo?: string): ContextNavigation | null {
  const parts = pathname.split('/').filter(Boolean)
  if (parts[0] === 'slots' && parts.length >= 2) return { to: returnTo ?? '/slots' + search, ariaKey: 'slots.backToSlots', titleKey: 'title.slotDetail' }
  if (parts[0] === 'servers' && parts[1]) {
    const serverPath = '/servers/' + parts[1]
    if (parts[2] === 'slots' && parts.length >= 4) return { to: returnTo ?? serverPath + '/slots', ariaKey: 'slots.backToSlots', titleKey: 'title.slotDetail' }
    if (parts[2] === 'slots') return { to: returnTo ?? serverPath, ariaKey: 'nav.goBackToServer', titleKey: 'title.serverSlots' }
    if (parts[2] === 'tasks' && parts.length >= 5) return { to: returnTo ?? serverPath + '/tasks', ariaKey: 'nav.goBackToTasks', titleKey: 'title.taskDetail' }
    if (parts[2] === 'tasks') return { to: returnTo ?? serverPath, ariaKey: 'nav.goBackToServer', titleKey: 'title.serverTasks' }
    if (parts[2] === 'agents' && parts.length >= 4) return { to: returnTo ?? serverPath + '/agents', ariaKey: 'agents.backToAgents', titleKey: 'nav.agents' }
    if (parts[2] === 'agents') return { to: returnTo ?? serverPath, ariaKey: 'nav.goBackToServer', titleKey: 'nav.agents' }
    if (parts[2] === 'context') return { to: returnTo ?? serverPath, ariaKey: 'nav.goBackToServer', titleKey: 'nav.context' }
    if (parts[2] === 'health') return { to: returnTo ?? serverPath, ariaKey: 'nav.goBackToServer', titleKey: 'nav.health' }
    return { to: returnTo ?? '/', ariaKey: 'nav.goBackToFleet', titleKey: 'title.server' }
  }
  if (parts[0] === 'meshes' && parts[1]) {
    const meshPath = '/meshes/' + parts[1]
    if (parts[2] === 'persistent' && parts.length >= 4) return { to: returnTo ?? meshPath + '/persistent', ariaKey: 'slots.backToSlots', titleKey: 'title.slotDetail' }
    if (parts[2] === 'persistent') return { to: returnTo ?? meshPath, ariaKey: 'nav.goBackToMesh', titleKey: 'title.serverSlots' }
    if (parts[2] === 'agents') return { to: returnTo ?? meshPath, ariaKey: 'nav.goBackToMesh', titleKey: 'nav.agents' }
    if (parts[2] === 'tasks') return { to: returnTo ?? meshPath, ariaKey: 'nav.goBackToMesh', titleKey: 'nav.tasks' }
    if (parts[2] === 'activity') return { to: returnTo ?? meshPath, ariaKey: 'nav.goBackToMesh', titleKey: 'title.activity' }
    return { to: returnTo ?? '/connections', ariaKey: 'nav.goBackToConnections', titleKey: 'title.mesh' }
  }
  if (pathname === '/activity') {
    const server = new URLSearchParams(search).get('server')
    if (server) return { to: returnTo ?? '/servers/' + encodeURIComponent(server), ariaKey: 'nav.goBackToServer', titleKey: 'title.activity' }
  }
  return null
}

export function AppShell({
  children,
  servers,
}: {
  children: ReactNode
  servers: FleetServerReadModel[]
}) {
  const location = useLocation()
  const { t, number } = useI18n()
  const [menuOpen, setMenuOpen] = useState(false)
  const toggleRef = useRef<HTMLButtonElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)

  const selectedServer = useMemo(() => {
    const pathCandidate = pathServer(location.pathname)
    const params = new URLSearchParams(location.search)
    const candidate = pathCandidate ?? (location.pathname.startsWith('/meshes/') ? undefined : params.get('server') ?? undefined)
    return candidate ? servers.find((server) => server.instanceId === candidate) : undefined
  }, [location.pathname, location.search, servers])

  const selectedMeshId = useMemo(() => {
    const match = /^\/meshes\/([^/]+)/.exec(location.pathname)
    if (!match) return undefined
    try { return decodeURIComponent(match[1]) } catch { return match[1] }
  }, [location.pathname])


  useEffect(() => {
    if (!menuOpen) return
    const focusTimer = window.setTimeout(() => closeRef.current?.focus(), 0)
    const close = () => setMenuOpen(false)
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      close()
      toggleRef.current?.focus()
    }
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('popstate', close)
    return () => {
      window.clearTimeout(focusTimer)
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('popstate', close)
    }
  }, [menuOpen])

  const current = activeKey(location.pathname)
  const contextual = contextNavigation(location.pathname, location.search, navigationReturnTo(location.state))
  const selectedId = selectedServer?.instanceId
  const serverState = selectedServer ? serverVisualState(selectedServer) : undefined
  const problemCount = servers.filter(needsAttention).length
  const liveCount = servers.filter((server) => server.connectivity === 'live').length
  const isServerOverview = /^\/servers\/[^/]+\/?$/.test(location.pathname)
  const isMeshOverview = /^\/meshes\/[^/]+\/?$/.test(location.pathname)
  const appBarVariant = isServerOverview || isMeshOverview ? 'detail' : contextual ? 'secondary' : 'root'
  const globalScreenTitle = location.pathname === '/'
    ? t('nav.fleet')
    : location.pathname === '/slots'
      ? t('nav.slots')
    : location.pathname === '/connections' || location.pathname === '/connect'
      ? t('nav.connections')
      : location.pathname === '/settings'
        ? t('settings.title')
        : t('app.console')
  const appBarTitle = isServerOverview
    ? selectedServer?.displayName ?? t('title.server')
    : contextual
      ? t(contextual.titleKey)
      : globalScreenTitle
  const appBarEyebrow = selectedServer?.displayName ?? (selectedMeshId ? t('title.mesh') + ' · ' + selectedMeshId : 'Terminal MCP')
  const isGlobalActive = (key: NavigationKey) => {
    if (key === 'fleet') return location.pathname === '/'
    if (key === 'slots') return location.pathname === '/slots' || location.pathname.startsWith('/slots/')
    if (key === 'connections') return location.pathname === '/connections' || location.pathname === '/connect'
    if (key === 'settings') return location.pathname === '/settings'
    return false
  }

  const destination = (item: NavigationItem) => item.globalPath
  const serverDestination = (item: NavigationItem) => selectedId && item.serverPath ? item.serverPath(selectedId) : item.globalPath
  const meshDestination = (item: NavigationItem) => selectedMeshId && item.meshPath ? item.meshPath(selectedMeshId) : item.globalPath
  const bottomNavigation: ResolvedNavigationItem[] = selectedId
    ? [
        { key: 'overview', labelKey: 'nav.overview', icon: 'server', to: '/servers/' + encodeURIComponent(selectedId), active: isServerOverview },
        ...serverNavigation.map((item) => ({ ...item, to: serverDestination(item), active: current === item.key })),
      ]
    : selectedMeshId
      ? [
          { key: 'overview', labelKey: 'nav.overview', icon: 'mesh', to: '/meshes/' + encodeURIComponent(selectedMeshId), active: isMeshOverview },
          ...meshNavigation.map((item) => ({ ...item, to: meshDestination(item), active: current === item.key })),
        ]
      : globalNavigation.map((item) => ({ ...item, to: destination(item), active: isGlobalActive(item.key) }))

  return (
    <div className={'app-shell' + (current === 'activity' ? ' app-shell-activity' : '')}>
      <header className={'topbar app-bar app-bar-' + appBarVariant} data-variant={appBarVariant}>
        <div className="app-bar-leading">
          <IconButton
            ref={toggleRef}
            className="menu-toggle"
            icon="menu"
            label={t('aria.openNavigation')}
            aria-expanded={menuOpen}
            aria-controls="global-navigation"
            onClick={() => setMenuOpen(true)}
          />
        </div>
        <div className="brand-block">
          <p className="eyebrow">{appBarEyebrow}</p>
          {appBarVariant === 'root' ? <h1>{appBarTitle}</h1> : <span className="app-bar-title">{appBarTitle}</span>}
        </div>
        <div className="app-bar-trailing">
          {selectedServer && serverState ? (
            <StatusBadge state={serverState} label={serverStatusLabel(serverState, t)} />
          ) : location.pathname !== '/' ? (
            <span className={'environment-badge' + (problemCount > 0 ? ' environment-badge-attention' : '')}>
              {problemCount > 0 ? t('fleet.needsAttention') + ' ' + number(problemCount) : t('fleet.live') + ' ' + number(liveCount)}
            </span>
          ) : null}
        </div>
      </header>

      {menuOpen ? (
        <button
          className="nav-scrim"
          type="button"
          aria-label={t('aria.closeNavigationOverlay')}
          onClick={() => {
            setMenuOpen(false)
            toggleRef.current?.focus()
          }}
        />
      ) : null}

      <div className="shell-grid">
        <nav
          id="global-navigation"
          className={'sidebar global-navigation' + (menuOpen ? ' open' : '')}
          aria-label={t('aria.applicationNavigation')}
        >
          <div className="navigation-heading">
            <div>
              <p className="eyebrow">{t('nav.navigation')}</p>
              <strong>{selectedServer ? selectedServer.displayName : selectedMeshId ? t('title.mesh') + ' · ' + selectedMeshId : t('nav.noServerSelected')}</strong>
            </div>
            <IconButton
              ref={closeRef}
              className="menu-close"
              icon="close"
              label={t('aria.closeNavigation')}
              onClick={() => {
                setMenuOpen(false)
                toggleRef.current?.focus()
              }}
            />
          </div>

          {globalNavigation.map((item) => (
            <Link
              key={item.key}
              to={destination(item)}
              aria-current={isGlobalActive(item.key) ? 'page' : undefined}
              className={isGlobalActive(item.key) ? 'nav-link active' : 'nav-link'}
              onClick={() => setMenuOpen(false)}
            >
              <Icon name={item.icon} className="nav-drawer-icon" />
              <span>{t(item.labelKey)}</span>
            </Link>
          ))}

          {selectedServer ? (
            <div className="navigation-context-group" aria-label={t('server.navigation')}>
              <Link
                className={/^\/servers\/[^/]+\/?$/.test(location.pathname) ? 'nav-link active' : 'nav-link'}
                to={'/servers/' + encodeURIComponent(selectedServer.instanceId)}
                onClick={() => setMenuOpen(false)}
              >
                <Icon name="server" className="nav-drawer-icon" />
                <span>{t('nav.overview')}</span>
              </Link>
              {serverNavigation.map((item) => (
                <Link
                  key={item.key}
                  to={serverDestination(item)}
                  aria-current={current === item.key ? 'page' : undefined}
                  className={current === item.key ? 'nav-link active' : 'nav-link'}
                  onClick={() => setMenuOpen(false)}
                >
                  <Icon name={item.icon} className="nav-drawer-icon" />
                  <span>{t(item.labelKey)}</span>
                </Link>
              ))}
            </div>
          ) : null}

          {selectedMeshId ? (
            <div className="navigation-context-group" aria-label={t('title.mesh') + ' · ' + selectedMeshId}>
              <Link className={isMeshOverview ? 'nav-link active' : 'nav-link'} to={'/meshes/' + encodeURIComponent(selectedMeshId)} onClick={() => setMenuOpen(false)}>
                <Icon name="mesh" className="nav-drawer-icon" />
                <span>{t('nav.overview')}</span>
              </Link>
              {meshNavigation.map((item) => (
                <Link
                  key={item.key}
                  className={current === item.key ? 'nav-link active' : 'nav-link'}
                  to={meshDestination(item)}
                  aria-current={current === item.key ? 'page' : undefined}
                  onClick={() => setMenuOpen(false)}
                >
                  <Icon name={item.icon} className="nav-drawer-icon" />
                  <span>{t(item.labelKey)}</span>
                </Link>
              ))}
            </div>
          ) : null}

          {menuOpen ? (
            <div className="navigation-server-list" aria-label={t('nav.servers')}>
              {servers.map((server) => (
                <Link
                  key={server.instanceId}
                  className={'navigation-server-link' + (server.instanceId === selectedId ? ' active' : '')}
                  to={serverSwitchDestination(location.pathname, location.search, server.instanceId)}
                  onClick={() => setMenuOpen(false)}
                >
                  {server.displayName}
                </Link>
              ))}
            </div>
          ) : null}
          {menuOpen && servers.length === 0 ? <p className="navigation-hint">{t('nav.chooseServerHint')}</p> : null}
          <small className="navigation-version">{APK_VERSION_LABEL}</small>
        </nav>

        <main className={'content' + (current === 'activity' ? ' content-activity' : '')}>
          {children}
        </main>
      </div>
      <nav className="mobile-bottom-navigation" aria-label={t('aria.bottomNavigation')}>
        {bottomNavigation.map((item) => (
          <Link
            key={item.key}
            to={item.to}
            aria-current={item.active ? 'page' : undefined}
            aria-label={t(item.labelKey) + ' · ' + t('aria.bottomNavigation')}
            className={item.active ? 'nav-link active' : 'nav-link'}
          >
            <span className="nav-icon" aria-hidden="true"><Icon name={item.icon} /></span>
            <span className="nav-label">{t(item.labelKey)}</span>
          </Link>
        ))}
      </nav>
    </div>
  )
}
