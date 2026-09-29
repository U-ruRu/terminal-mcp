import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'

import type { FleetServerReadModel } from '../fleet/readModel'
import type { MessageKey } from '../i18n/catalogs'
import { useI18n } from '../i18n/useI18n'

type NavigationItem = {
  key: 'fleet' | 'servers' | 'connections' | 'agents' | 'tasks' | 'activity' | 'context' | 'health' | 'settings'
  labelKey: MessageKey
  globalPath: string
  serverPath?: (instanceId: string) => string
}

const navigation: NavigationItem[] = [
  { key: 'fleet', labelKey: 'nav.fleet', globalPath: '/' },
  { key: 'servers', labelKey: 'nav.servers', globalPath: '/servers' },
  { key: 'connections', labelKey: 'nav.connections', globalPath: '/connections' },
  {
    key: 'agents',
    labelKey: 'nav.agents',
    globalPath: '/agents',
    serverPath: (instanceId) => '/servers/' + encodeURIComponent(instanceId) + '/agents',
  },
  {
    key: 'tasks',
    labelKey: 'nav.tasks',
    globalPath: '/tasks',
    serverPath: (instanceId) => '/servers/' + encodeURIComponent(instanceId) + '/tasks',
  },
  {
    key: 'activity',
    labelKey: 'nav.activity',
    globalPath: '/activity',
    serverPath: (instanceId) => '/activity?server=' + encodeURIComponent(instanceId),
  },
  {
    key: 'context',
    labelKey: 'nav.context',
    globalPath: '/context',
    serverPath: (instanceId) => '/servers/' + encodeURIComponent(instanceId) + '/context',
  },
  {
    key: 'health',
    labelKey: 'nav.health',
    globalPath: '/health',
    serverPath: (instanceId) => '/servers/' + encodeURIComponent(instanceId) + '/health',
  },
  { key: 'settings', labelKey: 'nav.settings', globalPath: '/settings' },
]

function pathServer(pathname: string): string | undefined {
  const match = /^\/servers\/([^/]+)/.exec(pathname)
  if (!match) return undefined
  try {
    return decodeURIComponent(match[1])
  } catch {
    return undefined
  }
}

function activeKey(pathname: string): NavigationItem['key'] {
  if (pathname === '/') return 'fleet'
  if (pathname === '/servers') return 'servers'
  if (pathname === '/activity') return 'activity'
  if (pathname === '/settings') return 'settings'
  if (pathname === '/connections') return 'connections'
  if (pathname === '/agents' || pathname.endsWith('/agents')) return 'agents'
  if (pathname === '/tasks' || pathname.includes('/tasks')) return 'tasks'
  if (pathname === '/context' || pathname.endsWith('/context')) return 'context'
  if (pathname === '/health' || pathname.endsWith('/health')) return 'health'
  if (/^\/servers\/[^/]+\/?$/.test(pathname)) return 'servers'
  return 'fleet'
}

function withContext(path: string, instanceId: string | undefined): string {
  if (!instanceId || path === '/activity') return path
  const separator = path.includes('?') ? '&' : '?'
  return path + separator + 'server=' + encodeURIComponent(instanceId)
}

type ContextNavigation = {
  to: string
  labelKey: 'nav.backToFleet' | 'nav.backToServer' | 'nav.backToTasks'
  ariaKey: 'nav.goBackToFleet' | 'nav.goBackToServer' | 'nav.goBackToTasks'
  titleKey: 'title.server' | 'title.serverTasks' | 'title.taskDetail' | 'title.activity'
}

function contextNavigation(pathname: string, search: string): ContextNavigation | null {
  const parts = pathname.split('/').filter(Boolean)
  if (parts[0] === 'servers' && parts[1]) {
    const serverPath = '/servers/' + encodeURIComponent(parts[1])
    if (parts[2] === 'tasks' && parts.length >= 5) return { to: serverPath + '/tasks', labelKey: 'nav.backToTasks', ariaKey: 'nav.goBackToTasks', titleKey: 'title.taskDetail' }
    if (parts[2] === 'tasks') return { to: serverPath, labelKey: 'nav.backToServer', ariaKey: 'nav.goBackToServer', titleKey: 'title.serverTasks' }
    return { to: '/', labelKey: 'nav.backToFleet', ariaKey: 'nav.goBackToFleet', titleKey: 'title.server' }
  }
  if (pathname === '/activity') {
    const server = new URLSearchParams(search).get('server')
    if (server) return { to: '/servers/' + encodeURIComponent(server), labelKey: 'nav.backToServer', ariaKey: 'nav.goBackToServer', titleKey: 'title.activity' }
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
  const { t } = useI18n()
  const [menuOpen, setMenuOpen] = useState(false)
  const toggleRef = useRef<HTMLButtonElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)

  const selectedServer = useMemo(() => {
    const params = new URLSearchParams(location.search)
    const candidate = pathServer(location.pathname) ?? params.get('server') ?? undefined
    return candidate ? servers.find((server) => server.instanceId === candidate) : undefined
  }, [location.pathname, location.search, servers])


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
  const contextual = contextNavigation(location.pathname, location.search)
  const selectedId = selectedServer?.instanceId

  const destination = (item: NavigationItem) => {
    if (selectedId && item.serverPath) return item.serverPath(selectedId)
    return withContext(item.globalPath, selectedId)
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <button
          ref={toggleRef}
          className="menu-toggle"
          type="button"
          aria-label={t('aria.openNavigation')}
          aria-expanded={menuOpen}
          aria-controls="global-navigation"
          onClick={() => setMenuOpen(true)}
        >
          <span aria-hidden="true">☰</span>
        </button>
        <div className="brand-block">
          <p className="eyebrow">Terminal MCP</p>
          <h1>{t('app.console')}</h1>
        </div>
        <span className="environment-badge">{t('environment.localFleet')}</span>
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
              <strong>{selectedServer ? selectedServer.displayName : t('nav.noServerSelected')}</strong>
            </div>
            <button
              ref={closeRef}
              className="menu-close"
              type="button"
              aria-label={t('aria.closeNavigation')}
              onClick={() => {
                setMenuOpen(false)
                toggleRef.current?.focus()
              }}
            >
              ×
            </button>
          </div>

          {navigation.map((item) => (
            <Link
              key={item.key}
              to={destination(item)}
              aria-current={current === item.key ? 'page' : undefined}
              className={current === item.key ? 'nav-link active' : 'nav-link'}
              onClick={() => setMenuOpen(false)}
            >
              {t(item.labelKey)}
            </Link>
          ))}

          {selectedServer ? (
            <Link className="selected-server-link" to={'/servers/' + encodeURIComponent(selectedServer.instanceId)} onClick={() => setMenuOpen(false)}>
              {t('nav.currentServer')}: {selectedServer.displayName}
            </Link>
          ) : (
            <p className="navigation-hint">{t('nav.chooseServerHint')}</p>
          )}
        </nav>

        <main className="content">
          {contextual ? (
            <div className="mobile-context" aria-label={t('aria.currentLocation')}>
              <Link className="mobile-back" aria-label={t(contextual.ariaKey)} to={contextual.to}>{t(contextual.labelKey)}</Link>
              <span>{t(contextual.titleKey)}</span>
            </div>
          ) : null}
          {children}
        </main>
      </div>
    </div>
  )
}
