import type { ReactNode } from 'react'
import { NavLink } from 'react-router-dom'

const navigation = [
  { to: '/', label: 'Overview' },
  { to: '/agents', label: 'Agents' },
  { to: '/tasks', label: 'Tasks' },
  { to: '/context', label: 'Context' },
  { to: '/activity', label: 'Activity' },
]

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Terminal MCP</p>
          <h1>Console</h1>
        </div>
        <span className="environment-badge">Local fleet</span>
      </header>
      <div className="shell-grid">
        <nav className="sidebar" aria-label="Console navigation">
          {navigation.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) => (isActive ? 'nav-link active' : 'nav-link')}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <main className="content">{children}</main>
      </div>
    </div>
  )
}
