import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import AlertPreviews from '../AlertPreviews.jsx'
import { useSignupOpen } from '../auth.jsx'
import BrandMark, { useBrandMood } from '../BrandMark.jsx'
import ChannelLogo from '../ChannelLogo.jsx'
import { CHANNELS } from '../channels.js'
import {
  AWS_ICONS,
  AwsReadouts,
  AwsRelay,
  DeployQuiet,
  OneAlertNotForty,
  VpcMap,
} from '../LandingAws.jsx'
import { NightReadouts, NightTraces } from '../NightMonitor.jsx'
import { useMediaQuery } from '../useMediaQuery.js'

// What a signed-out visitor sees at the site's root: what Watchly does, and
// the way in. Deep links still go to the sign-in form and back. It wears the
// sign-in page's dark night-shift look, and takes the mascot's moods as you
// scroll (see useMoodJourney below). Its cards follow the mouse, in index.css.
// The hero switches between websites and AWS infrastructure; the AWS chapter's
// visuals are in LandingAws.jsx.

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
  ...AWS_ICONS,
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

// What Watchly watches in AWS, as infra.js's KINDS and CHECKS_BY_KIND have it.
const AWS_KINDS = [
  {
    icon: 'server',
    title: 'EC2 servers',
    lead: 'In a public subnet or a private one, checked from inside the VPC.',
    points: [
      'Ping, a TCP port, or an HTTP health path',
      'At its private IP, or its public one over the internet',
      'Flagged the moment AWS no longer has it',
    ],
  },
  {
    icon: 'asg',
    title: 'Auto Scaling groups',
    lead: 'Every instance the group has in service, whichever ones those are now.',
    points: [
      'Health checks run on each instance in service',
      'Scaling in and out never looks like an outage',
      'A warning when it falls short of desired capacity',
    ],
  },
  {
    icon: 'lb',
    title: 'Load balancers',
    lead: 'Application and Network Load Balancers, internal or internet-facing.',
    points: [
      'HTTP through its listener, or a TCP port',
      "AWS's own verdict on every target behind it",
      'Unhealthy targets warn before users notice',
    ],
  },
  {
    icon: 'db',
    title: 'RDS databases',
    lead: 'Aurora included, and Watchly never logs in.',
    points: [
      'Its port, from inside the VPC',
      'What RDS says: available, stopped, storage full',
      'CloudWatch CPU, storage, memory, connections and replica lag',
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
    text: 'One alert the moment a site or an AWS resource fails, a few reminders while it stays down, then one when it recovers, with how long it was out.',
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
    text: 'Slow responses, packet loss, expiring certificates and changed DNS records each have an alert of their own, and so do unhealthy targets, a group short of capacity and a database running hot.',
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
      'Projects group sites, or AWS resources, with the people responsible for them',
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
  {
    icon: 'shield',
    title: 'Read-only in AWS',
    points: [
      'Every call it makes to AWS describes, lists or gets: nothing it can change',
      'Databases watched without a password',
      'Several AWS accounts, each through a role it assumes',
      'Probes never reach instance metadata, or Watchly itself',
    ],
  },
]

const STEPS = [
  {
    title: 'Create a project',
    text: 'For websites or for AWS infrastructure. Add the people responsible, and choose email, Slack, Telegram, WhatsApp or any mix.',
  },
  {
    title: 'Add what to watch',
    text: 'A URL, a host to ping or a DNS record. Or, for AWS, a server, Auto Scaling group, load balancer or database, picked from what Watchly finds in your VPC.',
  },
  {
    title: 'Hear about it first',
    text: 'Watchly checks around the clock and alerts the project the moment something is wrong, and again when it is fixed.',
  },
]

// What the hero pitches: the websites Watchly always watched, or the AWS
// infrastructure it watches from inside a VPC. A switch above the headline.
const SCOPES = [
  {
    id: 'web',
    label: 'Websites',
    title: 'Know the moment a site goes down.',
    lead: "Watchly checks your websites, servers and DNS records on their own schedule, alerts the right people by email, Slack, Telegram, WhatsApp or webhook, and tells them when it's back.",
  },
  {
    id: 'aws',
    label: 'AWS infrastructure',
    title: 'Know the moment a server goes down.',
    lead: "From inside your VPC, Watchly watches EC2 servers, Auto Scaling groups, load balancers and RDS databases, public or private, read-only, across every AWS account you add. Alerts go out the same way, and deploys page no one.",
  },
]

// The setup walkthrough lives in the repo's docs, so it is the same for every
// install and needs no sign-in to read.
const QUICK_START_URL = 'https://github.com/TahjibNil75/watchly/blob/main/doc/quick-start.md'

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

// The page's mood follows the scroll. Each chapter (a [data-mood] block in
// <main>) names a mood and what the mascot says there; the one at the middle
// of the screen sets the page's, and the mascot's, glow. The hero is the
// exception: it keeps the mascot's own clock-driven round, and only hands over
// once you scroll past it. The scroll also streams the grid floor and drifts
// each chapter's giant word, so it all runs backwards on the way up.
// Visitors who ask for reduced motion get the page as it opens, still.
const JOURNEY_QUERY = '(prefers-reduced-motion: reduce)'

function useMoodJourney() {
  const main = useRef(null)
  const floor = useRef(null)
  const still = useMediaQuery(JOURNEY_QUERY)
  const [chapter, setChapter] = useState({ mood: 'up', say: '' })
  useEffect(() => {
    if (still) return undefined
    let frame = 0
    const update = () => {
      frame = 0
      const el = main.current
      if (!el) return
      const vh = window.innerHeight
      let current = null
      for (const c of el.querySelectorAll('[data-mood]')) {
        const box = c.getBoundingClientRect()
        if (box.top < vh * 0.5) current = c
        const ghost = c.querySelector('.landing-ghost')
        if (ghost) {
          const through = Math.min(1, Math.max(0, (vh - box.top) / (vh + box.height)))
          ghost.style.transform = `translate3d(0, ${((through - 0.5) * -220).toFixed(1)}px, 0)`
        }
      }
      if (floor.current) {
        floor.current.style.backgroundPosition = `0 ${window.scrollY * 0.5}px, 0 ${window.scrollY * 0.5}px, 0 0`
      }
      const next = { mood: current?.dataset.mood ?? 'up', say: current?.dataset.say ?? '' }
      setChapter((old) => (old.mood === next.mood && old.say === next.say ? old : next))
    }
    const onScroll = () => {
      if (!frame) frame = requestAnimationFrame(update)
    }
    update()
    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', onScroll)
    return () => {
      window.removeEventListener('scroll', onScroll)
      window.removeEventListener('resize', onScroll)
      cancelAnimationFrame(frame)
    }
  }, [still])
  return { chapter, main, floor }
}

// The mascot's AWS line for a chapter's, said in turn with it. Chapters not
// here keep the one line.
const AWS_SAY = {
  'Hm. That took 2.4 s…': 'Target 2 of 3 is slow…',
  'Outage! Telling everyone.': 'orders-asg: 0 of 4 in service!',
  'Back up. Every check kept.': 'Back in service. 4 of 4.',
  'Tinkering. Alerts muted.': 'Deploying. Alerts held.',
}
const SAY_TURN_MS = 3500

// What the mascot says in a chapter: its line, then its AWS line, and again.
// Each turn is keyed by its line, so it pops in again.
function BuddySay({ say }) {
  const [aws, setAws] = useState(false)
  const other = AWS_SAY[say]
  useEffect(() => {
    if (!other) return undefined
    const timer = setInterval(() => setAws((a) => !a), SAY_TURN_MS)
    return () => clearInterval(timer)
  }, [other])
  const line = aws && other ? other : say
  return (
    <span className="landing-say" key={line}>
      {line}
    </span>
  )
}

// A chapter of the page: the mood it sets, what the mascot says there, and the
// giant word behind it. Bands are chapters that are also a stripe.
function Chapter({ mood, say, word, band, children }) {
  return (
    <div
      className={`landing-chapter${band ? ' landing-band' : ''}`}
      data-mood={mood}
      data-say={say}
    >
      <span className="landing-ghost" aria-hidden="true">
        {word}
      </span>
      {children}
    </div>
  )
}

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

function ScopeSwitch({ scope, onChange }) {
  return (
    <div className="landing-scope" role="group" aria-label="What Watchly watches">
      {SCOPES.map((s) => (
        <button
          key={s.id}
          type="button"
          className="landing-scope-option"
          aria-pressed={scope === s.id}
          onClick={() => onChange(s.id)}
        >
          {scope === s.id && <span className="live-pill-dot" />}
          {s.label}
        </button>
      ))}
    </div>
  )
}

export default function Landing() {
  // The whole page plays through the mascot's moods, as the sign-in page does:
  // its glow, and the hero's trace and readouts, change with them. In the hero
  // they follow the clock; below it, the chapter on screen.
  const clockMood = useBrandMood()
  const { chapter, main, floor } = useMoodJourney()
  const inHero = !chapter.say
  const mood = inHero ? clockMood : chapter.mood
  const [scopeId, setScope] = useState('web')
  const scope = SCOPES.find((s) => s.id === scopeId)
  return (
    <div className={`landing is-${mood}`}>
      <div className="landing-floor" ref={floor} aria-hidden="true" />
      <header className="landing-nav">
        <nav className="landing-nav-links" aria-label="Page sections">
          <a href="#checks">What it checks</a>
          <a href="#aws">AWS</a>
          <a href="#alerts">Alerts</a>
          <a href="#how">How it works</a>
          <a href={QUICK_START_URL} target="_blank" rel="noopener noreferrer">
            Quick start guide
          </a>
        </nav>
      </header>

      <main ref={main}>
        <div className="landing-hero-wrap" data-mood="up" {...POINTER}>
          <NightTraces />
          {scope.id === 'aws' ? <AwsReadouts mood={mood} /> : <NightReadouts mood={mood} />}
          <section className="landing-section landing-hero">
            <div className="landing-hero-text">
              <span className="brand brand-hero">
                <BrandMark mood={mood} />
                <span className="brand-word">Watchly</span>
              </span>
              <ScopeSwitch scope={scopeId} onChange={setScope} />
              {/* Keyed, so switching plays their rise again. */}
              <h1 key={`title-${scope.id}`}>{scope.title}</h1>
              <p key={`lead-${scope.id}`} className="landing-lead muted">
                {scope.lead}
              </p>
              <Actions />
              <p className="muted small landing-note">
                New accounts start with view access; an admin adds you to the projects you look
                after.
              </p>
              {scope.id === 'aws' ? (
                <AwsRelay down={mood === 'down'} />
              ) : (
                <Relay down={mood === 'down'} />
              )}
            </div>
          </section>
        </div>

        <Chapter band mood="slow" say="Hm. That took 2.4 s…" word="SLOW">
          <section className="landing-section" id="checks">
            <div className="landing-head">
              <h2>Websites, servers and DNS</h2>
              <p className="muted">
                Each check runs on its own interval, and each can be paused, edited or run by hand
                at any time.
              </p>
              <p className="landing-aws-link">
                <a href="#aws">On AWS? See what Watchly watches inside your VPC ↓</a>
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
        </Chapter>

        <Chapter mood="up" say="All healthy, to the last instance." word="VPC">
          <section className="landing-section" id="aws">
            <div className="landing-head">
              <h2>Inside your AWS, too</h2>
              <p className="muted">
                Run Watchly on an EC2 instance in your VPC and it checks what the internet
                can&apos;t reach. Resources come from AWS, found in the VPC or added by id, and stay
                in sync with it.
              </p>
            </div>
            <div className="landing-grid landing-grid-4">
              {AWS_KINDS.map((k) => (
                <article key={k.title} className="landing-card" {...POINTER}>
                  <Icon name={k.icon} />
                  <h3>{k.title}</h3>
                  <p className="muted">{k.lead}</p>
                  <Points items={k.points} />
                </article>
              ))}
            </div>
            <VpcMap />
          </section>
        </Chapter>

        <Chapter band mood="down" say="Outage! Telling everyone." word="DOWN">
          <section className="landing-section" id="alerts">
            <div className="landing-head">
              <h2>Alerts people act on</h2>
              <p className="muted">
                Enough to fix it, and no more: every alert carries the evidence, and repeats stop
                once everyone knows.
              </p>
            </div>
            <AlertPreviews />
            <OneAlertNotForty />
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
        </Chapter>

        <Chapter mood="up" say="Back up. Every check kept." word="UP">
          <section className="landing-section">
            <div className="landing-grid">
              {TEAM.map((t) => (
                <article key={t.title} className="landing-card" {...POINTER}>
                  <Icon name={t.icon} />
                  <h3>{t.title}</h3>
                  <Points items={t.points} />
                </article>
              ))}
            </div>
          </section>
        </Chapter>

        <Chapter band mood="maint" say="Tinkering. Alerts muted." word="FIX">
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
            <p className="landing-guide">
              <a
                href={QUICK_START_URL}
                className="btn btn-lg"
                target="_blank"
                rel="noopener noreferrer"
              >
                Read the quick start guide
              </a>
              <span className="muted small">
                Install, first project, first site, inviting your team.
              </span>
            </p>
            <DeployQuiet />
          </section>
        </Chapter>

        <Chapter mood="up" say="Back up. You heard first." word="UP">
          <section className="landing-section landing-closing">
            <div className="landing-head">
              <h2>Hear about it first.</h2>
              <p className="muted">
                The moment a site or server fails, and again the moment it is fixed.
              </p>
            </div>
            <Actions />
          </section>
        </Chapter>
      </main>

      <div className={`landing-buddy${inHero ? ' is-hidden' : ''}`} aria-hidden="true">
        {!inHero && <i className="landing-ripple" key={chapter.say} />}
        {!inHero && <BuddySay key={`say-${chapter.say}`} say={chapter.say} />}
        <BrandMark mood={mood} />
      </div>

      <footer className="landing-footer">
        <span className="brand">
          <BrandMark />
          <span className="brand-word">Watchly</span>
        </span>
        <span className="muted small">Uptime, ping, DNS and AWS infrastructure monitoring.</span>
      </footer>
    </div>
  )
}
