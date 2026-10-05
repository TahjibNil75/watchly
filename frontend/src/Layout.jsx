import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from './auth.jsx'
import BrandMark from './BrandMark.jsx'
import EventToasts from './EventToasts.jsx'
import { useInfraEnabled } from './infra.js'
import { canEditNotificationDefaults, canManageUsers } from './roles.js'

// 24x24 stroke icons, drawn inline so the sidebar needs no icon package.
const ICONS = {
  websites: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M3 9h18M6.5 6.5h.01M9.5 6.5h.01" />
    </>
  ),
  infra: (
    <>
      <rect x="3.5" y="4" width="17" height="6.5" rx="1.5" />
      <rect x="3.5" y="13.5" width="17" height="6.5" rx="1.5" />
      <path d="M7.5 7.25h.01M7.5 16.75h.01M11 7.25h5.5M11 16.75h5.5" />
    </>
  ),
  projects: (
    <path d="M20 17a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3.9a2 2 0 0 1-1.69-.9l-.81-1.2a2 2 0 0 0-1.67-.9H8a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2zM2 8v11a2 2 0 0 0 2 2h14" />
  ),
  notifications: (
    <>
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <path d="M12 9v4M12 17h.01" />
    </>
  ),
  users: (
    <>
      <circle cx="12" cy="7" r="3" />
      <path d="M6 20a6 6 0 0 1 12 0" />
      <circle cx="4.5" cy="9.5" r="2" />
      <path d="M1 19a4 4 0 0 1 3-3.5" />
      <circle cx="19.5" cy="9.5" r="2" />
      <path d="M23 19a4 4 0 0 0-3-3.5" />
    </>
  ),
  profile: (
    <>
      <path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </>
  ),
  signout: (
    <>
      <path d="M13 4h3a2 2 0 0 1 2 2v14M2 20h3M13 20h9M10 12v.01" />
      <path d="M13 4.56v16.16a1 1 0 0 1-1.24.97L5 20V5.56a2 2 0 0 1 1.52-1.94l4-1A2 2 0 0 1 13 4.56z" />
    </>
  ),
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

function Brand({ onClick }) {
  return (
    <NavLink to="/" className="brand" onClick={onClick}>
      <BrandMark />
      <span className="brand-word">Watchly</span>
    </NavLink>
  )
}

export default function Layout() {
  const { user, logout } = useAuth()
  const infra = useInfraEnabled()
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
    infra && { to: '/infra', label: 'Infrastructure', icon: 'infra' },
    { to: '/projects', label: 'Projects', icon: 'projects' },
    canEditNotificationDefaults(user) && {
      to: '/notifications',
      label: 'Notifications',
      icon: 'notifications',
    },
    canManageUsers(user) && { to: '/users', label: 'Users', icon: 'users' },
    { to: '/profile', label: 'Profile', icon: 'profile' },
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
          <button type="button" className="btn btn-block btn-danger-solid" onClick={logout}>
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

      <EventToasts userId={user.id} infra={Boolean(infra)} />
    </div>
  )
}
