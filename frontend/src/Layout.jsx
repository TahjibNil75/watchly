import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from './auth.jsx'
import BrandMark from './BrandMark.jsx'
import { Avatar } from './components.jsx'
import EventToasts from './EventToasts.jsx'
import { useDockerEnabled } from './docker.js'
import { useInfraEnabled } from './infra.js'
import { useNavCounts } from './navCounts.js'
import { canEditNotificationDefaults, canManageUsers } from './roles.js'

// 24x24 icons, drawn inline so the sidebar needs no icon package. The nav
// links are solid; their cut-out detail is drawn in --cut, the link's own
// background. Menu and account glyphs stay as strokes.
const SOLID_ICONS = {
  overview: (
    <>
      <rect x="3" y="3" width="8" height="8" rx="2" />
      <rect x="13" y="3" width="8" height="8" rx="2" />
      <rect x="3" y="13" width="8" height="8" rx="2" />
      <rect x="13" y="13" width="8" height="8" rx="2" />
    </>
  ),
  websites: (
    <>
      <circle cx="12" cy="12" r="10" />
      <g fill="none" stroke="var(--cut)" strokeWidth="1.6" strokeLinecap="round">
        <path d="M2.5 12h19" />
        <path d="M12 2.5c3 2.8 4.4 6 4.4 9.5s-1.4 6.7-4.4 9.5c-3-2.8-4.4-6-4.4-9.5S9 5.3 12 2.5z" />
      </g>
    </>
  ),
  infra: (
    <>
      <rect x="2.5" y="3" width="19" height="8" rx="2.2" />
      <rect x="2.5" y="13" width="19" height="8" rx="2.2" />
      <g fill="var(--cut)">
        <circle cx="6.8" cy="7" r="1.1" />
        <circle cx="6.8" cy="17" r="1.1" />
        <rect x="10.5" y="6.2" width="7" height="1.6" rx=".8" />
        <rect x="10.5" y="16.2" width="7" height="1.6" rx=".8" />
      </g>
    </>
  ),
  docker: (
    <>
      <path d="M2.4 12.5h18.8c.9 0 1.7-.9 1.9-2 .6 0 1.1-.3 1.4-.7-.6-.5-1.4-.6-2.1-.4-.4-1-1.1-1.6-1.8-1.9-.5.9-.6 2.2-.1 3.3H2.4z" />
      <path d="M2.7 13.5h17.8c-1.3 4.2-4.6 6.5-9.5 6.5-4.2 0-7.5-2.2-8.3-6.5z" />
      <rect x="5" y="9.3" width="2.8" height="2.8" rx=".5" />
      <rect x="8.3" y="9.3" width="2.8" height="2.8" rx=".5" />
      <rect x="11.6" y="9.3" width="2.8" height="2.8" rx=".5" />
      <rect x="8.3" y="6.1" width="2.8" height="2.8" rx=".5" />
      <rect x="11.6" y="6.1" width="2.8" height="2.8" rx=".5" />
    </>
  ),
  projects: (
    <path d="M4 20a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h4.3a2 2 0 0 1 1.7.9l.8 1.2a2 2 0 0 0 1.7.9H20a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2z" />
  ),
  notifications: (
    <>
      <path d="M12 2.5a6 6 0 0 0-6 6c0 5.5-2 7.2-3 8h18c-1-.8-3-2.5-3-8a6 6 0 0 0-6-6z" />
      <path d="M9.8 19.5h4.4a2.2 2.2 0 0 1-4.4 0z" />
    </>
  ),
}

const STROKE_ICONS = {
  users: (
    <>
      <circle cx="9" cy="8" r="3.5" />
      <path d="M2.5 20a6.5 6.5 0 0 1 13 0" />
      <path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18 14a6.5 6.5 0 0 1 3.5 6" />
    </>
  ),
  profile: (
    <>
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21a8 8 0 0 1 16 0" />
    </>
  ),
  signout: (
    <>
      <path d="M10 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h4" />
      <path d="m16 8 4 4-4 4M20 12H9" />
    </>
  ),
  chevron: <path d="m7 9 5 5 5-5" />,
  menu: <path d="M4 6h16M4 12h16M4 18h16" />,
  close: <path d="M6 6l12 12M18 6L6 18" />,
}

function Icon({ name }) {
  if (name in SOLID_ICONS) {
    return (
      <svg className="icon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        {SOLID_ICONS[name]}
      </svg>
    )
  }
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
      {STROKE_ICONS[name]}
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

// What a link's badge says: the monitor's problems in red, else its total.
function CountBadge({ counts }) {
  if (!counts) return null
  if (counts.problems > 0) {
    return (
      <span className="side-badge is-alarm">
        {counts.problems}
        <span className="visually-hidden"> need attention</span>
      </span>
    )
  }
  if (!counts.total) return null
  return <span className="side-badge">{counts.total}</span>
}

// The signed-in user at the foot of the sidebar; their menu holds the account
// pages and sign out.
function UserMenu({ user, logout, onNavigate }) {
  const [open, setOpen] = useState(false)
  const root = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const onPointer = (event) => root.current && !root.current.contains(event.target) && setOpen(false)
    const onKey = (event) => event.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', onPointer)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onPointer)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  const go = () => {
    setOpen(false)
    onNavigate()
  }

  return (
    <div className="user-menu" ref={root}>
      {open && (
        <div className="user-menu-pop" id="user-menu" role="menu">
          <NavLink to="/profile" role="menuitem" onClick={go}>
            <Icon name="profile" />
            Profile
          </NavLink>
          {canManageUsers(user) && (
            <NavLink to="/users" role="menuitem" onClick={go}>
              <Icon name="users" />
              Users
            </NavLink>
          )}
          <button type="button" role="menuitem" className="is-danger" onClick={logout}>
            <Icon name="signout" />
            Sign out
          </button>
        </div>
      )}
      <button
        type="button"
        className="user-menu-button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls="user-menu"
        onClick={() => setOpen(!open)}
      >
        <Avatar user={user} />
        <span className="user-menu-who">
          <strong>{user.full_name || user.username}</strong>
          <span className="muted small">{user.role}</span>
        </span>
        <Icon name="chevron" />
      </button>
    </div>
  )
}

export default function Layout() {
  const { user, logout } = useAuth()
  const infra = useInfraEnabled()
  const docker = useDockerEnabled()
  const counts = useNavCounts({ infra, docker })
  // Only meaningful on narrow screens, where the sidebar is a drawer.
  const [open, setOpen] = useState(false)
  const close = () => setOpen(false)

  useEffect(() => {
    if (!open) return undefined
    const onKey = (event) => event.key === 'Escape' && setOpen(false)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  const groups = [
    {
      label: 'Monitor',
      links: [
        { to: '/', label: 'Overview', icon: 'overview', end: true },
        { to: '/websites', label: 'Websites', icon: 'websites', counts: counts.websites },
        infra && { to: '/infra', label: 'Infrastructure', icon: 'infra', counts: counts.infra },
        docker && { to: '/docker', label: 'Docker', icon: 'docker', counts: counts.docker },
      ],
    },
    {
      label: 'Workspace',
      links: [
        { to: '/projects', label: 'Projects', icon: 'projects' },
        canEditNotificationDefaults(user) && {
          to: '/notifications',
          label: 'Notifications',
          icon: 'notifications',
        },
      ],
    },
  ].map((group) => ({ ...group, links: group.links.filter(Boolean) }))

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
          {groups.map((group) => (
            <div key={group.label} className="side-group" role="group" aria-label={group.label}>
              <div className="side-group-label" aria-hidden="true">
                {group.label}
              </div>
              {group.links.map(({ to, label, icon, end, counts: c }) => (
                <NavLink key={to} to={to} end={end} onClick={close}>
                  <Icon name={icon} />
                  {label}
                  <CountBadge counts={c} />
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        <div className="side-footer">
          <UserMenu user={user} logout={logout} onNavigate={close} />
        </div>
      </aside>

      <main className="main">
        <div className="container">
          <Outlet context={counts} />
        </div>
      </main>

      <EventToasts userId={user.id} infra={Boolean(infra)} />
    </div>
  )
}
