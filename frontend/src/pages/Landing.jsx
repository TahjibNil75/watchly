import { Link } from 'react-router-dom'
import AlertPreviews from '../AlertPreviews.jsx'
import { useSignupOpen } from '../auth.jsx'
import BrandMark, { useBrandMood } from '../BrandMark.jsx'
import ChannelLogo from '../ChannelLogo.jsx'
import { CHANNELS } from '../channels.js'
import { NightReadouts, NightTraces } from '../NightMonitor.jsx'

// What a signed-out visitor sees at the site's root: what Watchly does, and
// the way in. Deep links still go to the sign-in form and back. It wears the
// sign-in page's dark night-shift look. Its sections move with the scroll, and
// its cards follow the mouse, in index.css.

// 24x24 stroke icons, drawn inline like the sidebar's.
const ICONS = {
  http: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" />
    </>
  ),
  ping: (
    <>
      <rect x="3" y="4" width="18" height="7" rx="2" />
      <rect x="3" y="13" width="18" height="7" rx="2" />
      <path d="M7 7.5h.01M7 16.5h.01" />
    </>
  ),
  dns: <path d="M12 3v18M5 5h11l3 3-3 3H5zM19 13H8l-3 3 3 3h11z" />,
  channels: (
    <>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="m3 7 9 6 9-6" />
    </>
  ),
  thread: <path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12zM8 10h8M8 14h5" />,
  bell: <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0" />,
  warning: <path d="M12 3 2 20h20L12 3zM12 10v4M12 17h.01" />,
  retry: <path d="M3 12a9 9 0 0 1 15.5-6.2L21 8M21 3v5h-5M21 12a9 9 0 0 1-15.5 6.2L3 16M3 21v-5h5" />,
  edit: <path d="M4 20h4L19 9l-4-4L4 16v4zM13.5 6.5l4 4" />,
  users: (
    <>
      <circle cx="9" cy="8" r="3.5" />
      <path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 7M18 14.5a6.5 6.5 0 0 1 3.5 5.5" />
    </>
  ),
  chart: <path d="M3 3v18h18M7 15l4-4 3 3 5-6" />,
}

function Icon({ name }) {
  return (
    <span className={`landing-icon is-${name}`}>
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
    </span>
  )
}

const CHECKS = [
  {
    icon: 'http',
    title: 'Websites',
    lead: 'Request a URL on its own interval and judge the answer.',
    points: [
      'The status you expect, and text the page should or should not contain',
      'Where the time went: DNS, connect, TLS, first byte',
      'SSL certificate reminders at 14 and 7 days',
    ],
  },
  {
    icon: 'ping',
    title: 'Servers',
    lead: 'Ping hosts with no web server, or tell a network problem from an app one.',
    points: [
      'Round trips: minimum, average, maximum and jitter',
      'Packet loss on every check, charted and alerted on',
      'Down only when no ping is answered at all',
    ],
  },
  {
    icon: 'dns',
    title: 'DNS records',
    lead: 'A, AAAA, CNAME, MX and TXT, looked up at four public resolvers at once.',
    points: [
      'Down when a record stops resolving, or DNSSEC breaks',
      'Pin the values you expect: any other answer is an outage',
      'Or let Watchly learn them, and hear when they change',
    ],
  },
]

const ALERTS = [
  {
    icon: 'channels',
    title: 'Email, Slack, Telegram, WhatsApp and webhooks',
    text: 'Each project alerts by email, Slack, Telegram, WhatsApp or any mix, and a webhook can take every alert too. A site can add its own recipients, like a client, or its own Slack channel, Telegram chat or WhatsApp numbers.',
  },
  {
    icon: 'bell',
    title: 'Down, still down, back up',
    text: 'One alert the moment a site fails, a few reminders while it stays down, then one when it recovers, with how long it was out.',
  },
  {
    icon: 'thread',
    title: 'One thread per outage',
    text: 'In Slack the down alert starts a thread, reminders reply under it, and the recovery shows in the channel too. In Telegram they reply to the down alert.',
  },
  {
    icon: 'retry',
    title: 'Blips stay quiet',
    text: 'A failed check is tried again a few seconds later before anyone is alerted, so a one-off hiccup wakes no one.',
  },
  {
    icon: 'warning',
    title: 'Warnings before outages',
    text: 'Slow responses, packet loss, expiring certificates and changed DNS records each have an alert of their own.',
  },
  {
    icon: 'edit',
    title: 'Your wording',
    text: 'Edit the subject and body of every kind of alert, for all projects or just one, with placeholders for the details.',
  },
]

const TEAM = [
  {
    icon: 'users',
    title: 'Built for teams',
    points: [
      'Projects group sites with the people responsible for them',
      'Roles for admins, DevOps, project managers, developers and viewers',
      'Invite people by email; alert a client about their site alone',
    ],
  },
  {
    icon: 'chart',
    title: 'History to show for it',
    points: [
      'Uptime and response-time charts from 24 hours to 90 days',
      'The details of every recent check, and the history as a CSV download',
      'A monthly uptime report for each project, by email, Slack, Telegram and WhatsApp',
    ],
  },
]

const STEPS = [
  {
    title: 'Create a project',
    text: 'Name it, add the people responsible, and choose email, Slack, Telegram, WhatsApp or any mix.',
  },
  {
    title: 'Add what to watch',
    text: 'A URL, a host to ping or a DNS record, and how often to check it.',
  },
  {
    title: 'Hear about it first',
    text: 'Watchly checks around the clock and alerts the project the moment something is wrong, and again when it is fixed.',
  },
]

// Mouse only. Hands the pointer's place to CSS, which draws a spotlight at
// --mx/--my and tilts by --tilt-x/--tilt-y (each -0.5 to 0.5). No state, so
// moving re-renders nothing.
function followPointer(e) {
  if (e.pointerType !== 'mouse') return
  const el = e.currentTarget
  const box = el.getBoundingClientRect()
  const x = (e.clientX - box.left) / box.width
  const y = (e.clientY - box.top) / box.height
  el.style.setProperty('--mx', `${(x * 100).toFixed(1)}%`)
  el.style.setProperty('--my', `${(y * 100).toFixed(1)}%`)
  el.style.setProperty('--tilt-x', (0.5 - y).toFixed(3))
  el.style.setProperty('--tilt-y', (x - 0.5).toFixed(3))
}

function settle(e) {
  for (const name of ['--mx', '--my', '--tilt-x', '--tilt-y']) {
    e.currentTarget.style.removeProperty(name)
  }
}

const POINTER = { onPointerMove: followPointer, onPointerLeave: settle }

function Points({ items }) {
  return (
    <ul className="landing-points">
      {items.map((item) => (
        <li key={item}>{item}</li>
      ))}
    </ul>
  )
}

function Actions() {
  // Invite-only: signing in is the only way forward, so it takes the lead.
  const signupOpen = useSignupOpen() !== false
  return (
    <div className="landing-actions">
      {signupOpen && (
        <Link to="/signup" className="btn btn-primary btn-lg">
          Create an account
        </Link>
      )}
      <Link to="/login" className={signupOpen ? 'btn btn-lg' : 'btn btn-primary btn-lg'}>
        Sign in
      </Link>
    </div>
  )
}

// The hero's channel row. In an outage every channel lights up in turn, as on
// the demo board.
function Relay({ down }) {
  return (
    <div className={`live-channels landing-relay${down ? ' is-sending' : ''}`}>
      <span className="live-channels-label">
        {down ? 'Outage detected · alert sent to' : 'Alerts go out on any of these, and any mix of them'}
      </span>
      <div className="live-channels-list">
        {CHANNELS.map((c, i) => (
          <div
            key={c.id}
            className={down ? 'live-channel is-sent' : 'live-channel'}
            style={{ '--order': i }}
          >
            <ChannelLogo channel={c.id} />
            {c.name}
          </div>
        ))}
      </div>
    </div>
  )
}

export default function Landing() {
  // The whole page plays through the mascot's moods, as the sign-in page does:
  // its glow, and the hero's trace and readouts, change with them.
  const mood = useBrandMood()
  return (
    <div className={`landing is-${mood}`}>
      <header className="landing-nav">
        <nav className="landing-nav-links" aria-label="Page sections">
          <a href="#checks">What it checks</a>
          <a href="#alerts">Alerts</a>
          <a href="#how">How it works</a>
        </nav>
      </header>

      <main>
        <div className="landing-hero-wrap" {...POINTER}>
          <NightTraces />
          <NightReadouts mood={mood} />
          <section className="landing-section landing-hero">
            <div className="landing-hero-text">
              <BrandMark mood={mood} />
              <span className="live-pill">
                <span className="live-pill-dot" />
                Uptime, ping and DNS monitoring
              </span>
              <h1>Know the moment a site goes down.</h1>
              <p className="landing-lead muted">
                Watchly checks your websites, servers and DNS records on their own schedule,
                alerts the right people by email, Slack, Telegram, WhatsApp or webhook, and tells
                them when it&apos;s back.
              </p>
              <Actions />
              <p className="muted small landing-note">
                New accounts start with view access; an admin adds you to the projects you look
                after.
              </p>
              <Relay down={mood === 'down'} />
            </div>
          </section>
        </div>

        <section className="landing-section" id="checks">
          <div className="landing-head">
            <h2>Three ways to watch</h2>
            <p className="muted">
              Each check runs on its own interval, and each can be paused, edited or run by hand
              at any time.
            </p>
          </div>
          <div className="landing-grid">
            {CHECKS.map((c) => (
              <article key={c.title} className="landing-card" {...POINTER}>
                <Icon name={c.icon} />
                <h3>{c.title}</h3>
                <p className="muted">{c.lead}</p>
                <Points items={c.points} />
              </article>
            ))}
          </div>
        </section>

        <div className="landing-band">
          <section className="landing-section" id="alerts">
            <div className="landing-head">
              <h2>Alerts people act on</h2>
              <p className="muted">
                Enough to fix it, and no more: every alert carries the evidence, and repeats stop
                once everyone knows.
              </p>
            </div>
            <AlertPreviews />
            <div className="landing-grid landing-features">
              {ALERTS.map((a) => (
                <div key={a.title} className="landing-feature">
                  <Icon name={a.icon} />
                  <div>
                    <h3>{a.title}</h3>
                    <p className="muted">{a.text}</p>
                  </div>
                </div>
              ))}
            </div>
          </section>
        </div>

        <section className="landing-section">
          <div className="landing-grid landing-grid-2">
            {TEAM.map((t) => (
              <article key={t.title} className="landing-card" {...POINTER}>
                <Icon name={t.icon} />
                <h3>{t.title}</h3>
                <Points items={t.points} />
              </article>
            ))}
          </div>
        </section>

        <div className="landing-band">
          <section className="landing-section" id="how">
            <div className="landing-head">
              <h2>How it works</h2>
            </div>
            <ol className="landing-steps">
              {STEPS.map((s, i) => (
                <li key={s.title} {...POINTER}>
                  <span className="landing-step-num" aria-hidden="true">
                    {i + 1}
                  </span>
                  <h3>{s.title}</h3>
                  <p className="muted">{s.text}</p>
                </li>
              ))}
            </ol>
          </section>
        </div>
      </main>

      <footer className="landing-footer">
        <span className="brand">
          <BrandMark />
          <span className="brand-word">Watchly</span>
        </span>
        <span className="muted small">Uptime, ping and DNS monitoring.</span>
      </footer>
    </div>
  )
}
