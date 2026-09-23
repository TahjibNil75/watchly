import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { api } from './api.js'
import { useAuth } from './auth.jsx'
import { canEditNotificationDefaults, canManageUsers } from './roles.js'
import { useApi } from './useApi.js'

// 24x24 stroke icons, drawn inline so the sidebar needs no icon package.
const ICONS = {
  websites: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" />
    </>
  ),
  projects: <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />,
  notifications: (
    <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0" />
  ),
  users: (
    <>
      <circle cx="9" cy="8" r="3.5" />
      <path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 7M18 14.5a6.5 6.5 0 0 1 3.5 5.5" />
    </>
  ),
  signout: <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9" />,
  menu: <path d="M4 6h16M4 12h16M4 18h16" />,
  close: <path d="M6 6l12 12M18 6L6 18" />,
}

function Icon({ name }) {
  return (
    <svg
      className="icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {ICONS[name]}
    </svg>
  )
}

function MonitoringIndicator() {
  const { data, error } = useApi(() => api.health(), [], { pollMs: 60000 })
  let state = 'unknown'
  let label = 'Checking API…'
  if (error) {
    state = 'down'
    label = 'API unreachable'
  } else if (data) {
    state = data.monitoring === 'on' ? 'up' : 'paused'
    label = data.monitoring === 'on' ? 'Monitoring on' : 'Monitoring off'
  }
  return (
    <span className="health" title={label}>
      <span className={`dot dot-${state}`} />
      <span>{label}</span>
    </span>
  )
}

function Brand({ onClick }) {
  return (
    <NavLink to="/" className="brand" onClick={onClick}>
      <span className="brand-mark" aria-hidden="true" />
      Watchly
    </NavLink>
  )
}

export default function Layout() {
  const { user, logout } = useAuth()
  // Only meaningful on narrow screens, where the sidebar is a drawer.
  const [open, setOpen] = useState(false)
  const close = () => setOpen(false)

  useEffect(() => {
    if (!open) return undefined
    const onKey = (event) => event.key === 'Escape' && setOpen(false)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  const links = [
    { to: '/', label: 'Websites', icon: 'websites', end: true },
    { to: '/projects', label: 'Projects', icon: 'projects' },
    canEditNotificationDefaults(user) && {
      to: '/notifications',
      label: 'Notifications',
      icon: 'notifications',
    },
    canManageUsers(user) && { to: '/users', label: 'Users', icon: 'users' },
  ].filter(Boolean)

  return (
    <div className="app">
      <header className="topbar">
        <Brand />
        <button
          type="button"
          className="btn btn-ghost btn-sm menu-button"
          aria-label={open ? 'Close menu' : 'Open menu'}
          aria-expanded={open}
          aria-controls="sidebar"
          onClick={() => setOpen(!open)}
        >
          <Icon name={open ? 'close' : 'menu'} />
        </button>
      </header>

      {open && <div className="scrim" onClick={close} aria-hidden="true" />}

      <aside id="sidebar" className={open ? 'sidebar open' : 'sidebar'}>
        <div className="sidebar-brand">
          <Brand onClick={close} />
        </div>

        <nav className="side-links" aria-label="Main">
          {links.map(({ to, label, icon, end }) => (
            <NavLink key={to} to={to} end={end} onClick={close}>
              <Icon name={icon} />
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="side-footer">
          <MonitoringIndicator />
          <div className="who">
            <span className="who-name">{user.full_name || user.username}</span>
            <span className="role">{user.role}</span>
          </div>
          <button type="button" className="btn btn-block" onClick={logout}>
            <Icon name="signout" />
            Sign out
          </button>
        </div>
      </aside>

      <main className="main">
        <div className="container">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
