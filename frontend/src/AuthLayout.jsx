import { Outlet } from 'react-router-dom'
import LiveDemo from './LiveDemo.jsx'
import { useMediaQuery } from './useMediaQuery.js'

// Frame for the public pages (sign in, sign up, password reset, invitations).
// On wide screens a live demo board beside the form shows what Watchly does.

const SPLIT_QUERY = '(min-width: 960px)'

function Showcase() {
  return (
    <aside className="auth-aside">
      <div className="auth-pitch">
        <span className="live-pill">
          <span className="live-pill-dot" />
          Uptime monitoring
        </span>
        <h2>Know the moment a site goes down.</h2>
        <p className="muted">
          Watchly checks each of your websites on its own schedule, alerts the right people by
          email, Slack or webhook, and tells them when it&apos;s back.
        </p>
      </div>
      <LiveDemo />
    </aside>
  )
}

export default function AuthLayout() {
  const split = useMediaQuery(SPLIT_QUERY)
  return (
    <div className={split ? 'auth-layout is-split' : 'auth-layout'}>
      <Outlet />
      {split && <Showcase />}
    </div>
  )
}
