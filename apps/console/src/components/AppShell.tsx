import type { ReactNode } from 'react'
import { Link, NavLink, useLocation } from 'react-router-dom'

const navigation = [
  { to: '/', label: 'Overview' },
  { to: '/agents', label: 'Agents' },
  { to: '/tasks', label: 'Tasks' },
  { to: '/context', label: 'Context' },
  { to: '/activity', label: 'Activity' },
]

type ContextNavigation = { to: string; label: string; title: string }

function contextNavigation(pathname: string, search: string): ContextNavigation | null {
  const parts = pathname.split('/').filter(Boolean)
  if (parts[0] === 'servers' && parts[1]) {
    const serverPath = '/servers/' + encodeURIComponent(parts[1])
    if (parts[2] === 'tasks' && parts.length >= 5) {
      return { to: serverPath + '/tasks', label: 'Back to tasks', title: 'Task detail' }
    }
    if (parts[2] === 'tasks') {
      return { to: serverPath, label: 'Back to server', title: 'Server tasks' }
    }
    return { to: '/', label: 'Back to fleet', title: 'Server' }
  }
  if (pathname === '/activity') {
    const server = new URLSearchParams(search).get('server')
    if (server) {
      return { to: '/servers/' + encodeURIComponent(server), label: 'Back to server', title: 'Activity' }
    }
  }
  return null
}

export function AppShell({ children }: { children: ReactNode }) {
  const location = useLocation()
  const contextual = contextNavigation(location.pathname, location.search)
  return (
    <div className="app-shell">
      <header className="topbar">
        <div><p className="eyebrow">Terminal MCP</p><h1>Console</h1></div>
        <span className="environment-badge">Local fleet</span>
      </header>
      <div className="shell-grid">
        <nav className="sidebar" aria-label="Console navigation">
          {navigation.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.to === '/'} className={({ isActive }) => (isActive ? 'nav-link active' : 'nav-link')}>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <main className="content">
          {contextual ? <div className="mobile-context" aria-label="Current location"><Link className="mobile-back" aria-label={contextual.label.replace('Back to', 'Go back to')} to={contextual.to}>{contextual.label}</Link><span>{contextual.title}</span></div> : null}
          {children}
        </main>
      </div>
    </div>
  )
}
