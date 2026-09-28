import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { isDns, isPing, recordText, rtt } from '../checkTypes.js'
import {
  CheckTypeBadge,
  Empty,
  EnvironmentBadge,
  ErrorBanner,
  Loading,
  PersonList,
  StatusBadge,
  UserChecklist,
} from '../components.jsx'
import { dateTime, duration, percent, since, timeAgo, until } from '../format.js'
import Maintenance from '../Maintenance.jsx'
import { canManageProject } from '../roles.js'
import SiteHistory from '../SiteHistory.jsx'
import { useApi } from '../useApi.js'
import WebsiteForm from '../WebsiteForm.jsx'

const CHECKS_SHOWN = 60

// A failed connection has no response, so its recorded time (often 0) means
// nothing. A ping's time is its average round trip, when anything came back,
// and a DNS check's the resolvers' average answer time.
const responseTime = (c) =>
  c.ping
    ? c.ping.avg_ms
    : c.dns || c.status_code != null
      ? c.response_time_ms
      : null

// Up, but some pings went unanswered.
const lossy = (c) => c.is_up && c.ping?.loss_percent > 0

// Up, but the resolvers do not all give the same answer.
const split = (c) => c.is_up && c.dns?.consistent === false

const degraded = (c) => lossy(c) || split(c)

// "203.0.113.10, 203.0.113.11", TXT values quoted.
const recordsText = (type, records) => records.map((r) => recordText(type, r)).join(', ')

// What one resolver answered, in one line.
const answerText = (type, a) => (a.records ? recordsText(type, a.records) : a.error)

// Which resolvers' answers stand out: for a pinned check, any answer but the
// pinned values; otherwise any answer but the most common one.
function oddAnswers(d) {
  const key = (a) => (a.records ? a.records.join('\n') : a.error_type)
  const answered = d.answers.filter((a) => a.records)
  const denies = (a) => answered.length > 0 && ['nxdomain', 'no_records'].includes(a.error_type)
  if (d.expected.length) {
    const want = d.expected.join('\n')
    return new Set(d.answers.filter((a) => (a.records && key(a) !== want) || denies(a)))
  }
  const counts = new Map()
  for (const a of answered) counts.set(key(a), (counts.get(key(a)) ?? 0) + 1)
  const common = [...counts].sort((x, y) => y[1] - x[1])[0]?.[0]
  return new Set(d.answers.filter((a) => (a.records && key(a) !== common) || denies(a)))
}

// "3/5 replies · 40% lost", or "5/5 replies".
const packets = (p) =>
  `${p.received}/${p.sent} replies${p.received < p.sent ? ` · ${p.loss_percent}% lost` : ''}`

const STEPS = [
  ['dns_ms', 'DNS'],
  ['connect_ms', 'connect'],
  ['tls_ms', 'TLS'],
  ['first_byte_ms', 'first byte'],
]

// Where a check's time went, e.g. "DNS 12 ms · connect 40 ms · …"; empty when untimed.
const timeSplit = (c) =>
  STEPS.filter(([key]) => c[key] != null)
    .map(([key, label]) => `${label} ${c[key]} ms`)
    .join(' · ')

function CheckStrip({ checks }) {
  // Oldest on the left, like a status page. Empty slots on the left keep bar
  // widths steady while history builds up.
  const ordered = [...checks].reverse()
  const slowest = Math.max(1, ...ordered.map((c) => responseTime(c) ?? 0))
  const padding = Math.max(0, CHECKS_SHOWN - ordered.length)
  return (
    <div className="strip" aria-label="Recent checks, oldest first">
      {Array.from({ length: padding }, (_, i) => (
        <span key={`empty-${i}`} className="strip-bar is-empty" />
      ))}
      {ordered.map((c) => (
        <span
          key={c.id}
          className={`strip-bar ${degraded(c) ? 'is-degraded' : c.is_up ? 'is-up' : 'is-down'}`}
          style={{ height: `${c.is_up ? 25 + (75 * (responseTime(c) ?? 0)) / slowest : 100}%` }}
          title={`${dateTime(c.checked_at)} · ${c.is_up ? 'up' : 'down'}${
            responseTime(c) != null
              ? ` · ${c.ping ? rtt(responseTime(c)) : `${responseTime(c)} ms`}`
              : ''
          }${lossy(c) ? ` · ${c.ping.loss_percent}% packet loss` : ''}${
            split(c) ? ' · resolvers disagree' : ''
          }${c.error ? ` · ${c.error}` : ''}`}
        />
      ))}
    </div>
  )
}

const COLLAPSED_ROWS = 10

// A ping check's row: packets instead of an HTTP status, round trips instead
// of a response time.
function PingCells({ check: c, host }) {
  const p = c.ping
  return (
    <>
      <td className={`nowrap${lossy(c) ? ' text-pending' : ''}`}>{p ? packets(p) : '—'}</td>
      <td
        className="nowrap"
        title={
          p?.avg_ms != null
            ? `min ${rtt(p.min_ms)} · avg ${rtt(p.avg_ms)} · max ${rtt(p.max_ms)}` +
              (p.jitter_ms != null ? ` · jitter ${rtt(p.jitter_ms)}` : '')
            : undefined
        }
      >
        {rtt(p?.avg_ms)}
      </td>
      <td className="muted small">
        {c.error ?? ''}
        {/* What a host name resolved to. */}
        {p?.address && p.address !== host && <div className="truncate">→ {p.address}</div>}
      </td>
    </>
  )
}

// A DNS check's row: the records the resolvers agreed on instead of an HTTP
// status, their average answer time instead of a response time.
function DnsCells({ check: c }) {
  const d = c.dns
  const byResolver = d?.answers
    .map((a) => `${a.resolver}: ${answerText(d.record_type, a)}`)
    .join('\n')
  return (
    <>
      <td className={split(c) ? 'text-pending' : undefined} title={byResolver}>
        <div className="truncate">
          {d?.records
            ? recordsText(d.record_type, d.records)
            : d && !d.consistent
              ? 'resolvers disagree'
              : '—'}
        </div>
      </td>
      <td className="nowrap">{c.response_time_ms != null ? `${c.response_time_ms} ms` : '—'}</td>
      <td className="muted small">{c.error ?? ''}</td>
    </>
  )
}

function Checks({ checks, site }) {
  const [expanded, setExpanded] = useState(false)
  const [failedOnly, setFailedOnly] = useState(false)
  if (!checks.length) return <Empty>No checks yet. The first one runs within a minute.</Empty>
  const ping = isPing(site)
  const dns = isDns(site)
  const failed = checks.filter((c) => !c.is_up)
  const shown = failedOnly ? failed : checks

  return (
    <>
      <div className="tabs" role="tablist" aria-label="Which checks">
        {[
          [false, `All ${checks.length}`],
          [true, `Failed ${failed.length}`],
        ].map(([value, label]) => (
          <button
            key={label}
            type="button"
            role="tab"
            aria-selected={failedOnly === value}
            className={failedOnly === value ? 'tab active' : 'tab'}
            onClick={() => setFailedOnly(value)}
          >
            {label}
          </button>
        ))}
      </div>
      {!shown.length ? (
        <Empty>None of the last {checks.length} checks failed.</Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Result</th>
                <th>{ping ? 'Packets' : dns ? 'Records' : 'HTTP'}</th>
                <th>{ping ? 'Round trip' : dns ? 'Answer time' : 'Response'}</th>
                <th>Error</th>
              </tr>
            </thead>
            <tbody>
              {(expanded ? shown : shown.slice(0, COLLAPSED_ROWS)).map((c) => (
                <tr key={c.id}>
                  <td className="nowrap" title={dateTime(c.checked_at)}>
                    {timeAgo(c.checked_at)}
                  </td>
                  <td>
                    <StatusBadge status={c.is_up ? 'up' : 'down'} />
                  </td>
                  {ping ? (
                    <PingCells check={c} host={site.url} />
                  ) : dns ? (
                    <DnsCells check={c} />
                  ) : (
                    <>
                      <td>{c.status_code ?? '—'}</td>
                      <td className="nowrap" title={timeSplit(c) || undefined}>
                        {responseTime(c) != null ? `${responseTime(c)} ms` : '—'}
                      </td>
                      <td className="muted small">
                        {c.error ?? ''}
                        {c.final_url && <div className="truncate">→ {c.final_url}</div>}
                      </td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {shown.length > COLLAPSED_ROWS && (
        <button
          type="button"
          className="btn btn-sm show-more"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? 'Show fewer' : `Show all ${shown.length} checks`}
        </button>
      )}
    </>
  )
}

// What each resolver answered at the latest check.
function Resolvers({ check }) {
  const d = check?.dns
  if (!d) return null
  const odd = oddAnswers(d)
  return (
    <section className="card">
      <h2>Resolvers</h2>
      <p className="muted small">
        What each resolver answered at the latest check, {timeAgo(check.checked_at)}.
        {!d.consistent &&
          ' They disagree: a change may still be propagating, or some of them are being served other records.'}
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Resolver</th>
              <th>Answer</th>
              <th>TTL</th>
              <th>Time</th>
            </tr>
          </thead>
          <tbody>
            {d.answers.map((a) => (
              <tr key={a.address}>
                <td className="nowrap">
                  <strong>{a.resolver}</strong>
                  {a.resolver !== a.address && <div className="muted small">{a.address}</div>}
                </td>
                <td className={odd.has(a) ? (d.expected.length ? 'text-down' : 'text-pending') : undefined}>
                  {a.records
                    ? a.records.map((r) => (
                        <div key={r} className="record">
                          {recordText(d.record_type, r)}
                        </div>
                      ))
                    : a.error}
                </td>
                <td className="num nowrap">{a.ttl != null ? duration(a.ttl) : '—'}</td>
                <td className="num nowrap">{a.time_ms != null ? `${a.time_ms} ms` : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

// Whole days until the certificate ends; negative once it has.
const sslDaysLeft = (iso) => Math.floor((new Date(iso).getTime() - Date.now()) / 86_400_000)

const shortDate = (iso) =>
  new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })

// 24x24 stroke icons, drawn like the dashboard's.
function Icon({ className, children }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.9"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  )
}

const STATE_ICONS = {
  up: <path d="M5 12.5l4.5 4.5L19 7.5" />,
  down: <path d="M12 3.5 2.5 20h19zM12 10v4M12 17v.01" />,
  pending: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  maintenance: <path d="m18.9 8.1 2 .6a4.5 4.5 0 1 1-3.2-5.6l-.6 2zM13.3 10.7l-8.7 8.7" />,
  paused: <path d="M9 5v14M15 5v14" />,
}

const TILE_ICONS = {
  uptime: <path d="M3 12h4l2.5-6 5 12 2.5-6h4" />,
  speed: (
    <>
      <circle cx="12" cy="13" r="8" />
      <path d="M12 9v4l2.5 2.5M9.5 2.5h5" />
    </>
  ),
  failed: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="m9 9 6 6M15 9l-6 6" />
    </>
  ),
  ssl: (
    <>
      <rect x="5" y="11" width="14" height="10" rx="2" />
      <path d="M8 11V7a4 4 0 0 1 8 0v4" />
    </>
  ),
  loss: <path d="M4 20v-2M9 20v-6M14 20V9M19 20V4" />,
  resolvers: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" />
    </>
  ),
}

// The site's state in the dashboard's colours. A pause or a maintenance
// window outranks the status, which is not being checked meanwhile.
const siteTone = (s) =>
  !s.is_enabled
    ? 'paused'
    : s.maintenance
      ? 'maintenance'
      : s.status === 'unknown'
        ? 'pending'
        : s.status

// One line on where the site stands, its state word in its colour.
function StatusLine({ site: s }) {
  if (!s.is_enabled) {
    return (
      <>
        <strong>Paused</strong> · not being checked, and nobody is alerted
      </>
    )
  }
  if (s.maintenance) {
    return (
      <>
        <strong>In maintenance</strong> ·{' '}
        <span title={dateTime(s.maintenance.ends_at)}>ends in {until(s.maintenance.ends_at)}</span>
        {s.maintenance.reason && <> · {s.maintenance.reason}</>}
      </>
    )
  }
  if (s.status === 'down') {
    return (
      <>
        <strong>Down{s.down_since && ` for ${since(s.down_since)}`}</strong>
        {s.down_since && <> · since {dateTime(s.down_since)}</>} · {s.consecutive_failures}{' '}
        failed {s.consecutive_failures === 1 ? 'check' : 'checks'} · {s.down_alerts_sent} of{' '}
        {s.max_down_alerts} alerts sent
      </>
    )
  }
  if (s.status === 'unknown') {
    return (
      <>
        <strong>Pending</strong> · the first check runs within a minute
      </>
    )
  }
  return (
    <>
      <strong>Up</strong> ·{' '}
      <span title={dateTime(s.last_checked_at)}>checked {timeAgo(s.last_checked_at)}</span> ·
      every {duration(s.check_interval_seconds)}
    </>
  )
}

// "58 up · 2 down", over the checks in the strip.
function tally(checks) {
  const down = checks.filter((c) => !c.is_up).length
  const odd = checks.filter(degraded).length
  return [
    `${checks.length - down - odd} up`,
    odd > 0 && `${odd} degraded`,
    down > 0 && `${down} down`,
  ]
    .filter(Boolean)
    .join(' · ')
}

// The page's head: what the site is, where it stands and its last checks,
// tinted with its state's colour. `children` are the actions.
function SiteHero({ site: s, checks, children }) {
  const tone = siteTone(s)
  const latest = checks[0]
  return (
    <section className={`site-hero tone-${tone}`}>
      <div className="site-hero-head">
        <span className={`site-hero-icon${tone === 'down' ? ' is-alarm' : ''}`}>
          <Icon>{STATE_ICONS[tone]}</Icon>
        </span>
        <div className="site-hero-title">
          <h1>
            {s.name}
            <EnvironmentBadge environment={s.environment} />
            <CheckTypeBadge site={s} />
          </h1>
          <p className="site-hero-url muted">
            {isPing(s) ? (
              <code>{s.url}</code>
            ) : isDns(s) ? (
              <>
                <code>{s.url}</code> · {s.dns_record_type} record
              </>
            ) : (
              <a href={s.url} target="_blank" rel="noreferrer">
                {s.url}
                <Icon className="site-hero-out">
                  <path d="M7 17 17 7M8 7h9v9" />
                </Icon>
              </a>
            )}
          </p>
        </div>
        {children && <div className="actions">{children}</div>}
      </div>

      <div className="site-hero-body">
        <p className="site-hero-status">
          <StatusLine site={s} />
        </p>
        {checks.length > 0 && (
          <p className="site-hero-tally muted small">
            Last {checks.length} checks · {tally(checks)}
          </p>
        )}
      </div>
      {tone === 'down' && latest && !latest.is_up && latest.error && (
        <p className="site-hero-error">{latest.error}</p>
      )}
      <CheckStrip checks={checks} />
    </section>
  )
}

function StatTile({ label, value, note, tone, icon }) {
  return (
    <div className={`site-stat tone-${tone}`}>
      <span className="site-stat-label">{label}</span>
      <span className="site-stat-main">
        <span className="site-stat-value">{value}</span>
        <Icon className="site-stat-icon">{icon}</Icon>
      </span>
      <span className="site-stat-note">{note}</span>
    </div>
  )
}

const uptimeTone = (value) =>
  value == null ? 'paused' : value === 100 ? 'up' : value >= 99 ? 'pending' : 'down'

// The figures at a glance: uptime, speed and failures over a day, then what
// matters most for the kind of check: the certificate, packet loss or the
// resolvers. `day` and `month` are the 24-hour and 30-day stats, once loaded.
function SiteStats({ site: s, latest, day, month }) {
  const ping = isPing(s)
  const dns = isDns(s)
  const blank = '–'

  const time = latest ? responseTime(latest) : null
  const slow = time != null && s.slow_threshold_ms && time > s.slow_threshold_ms
  const failed = day ? day.checks - day.up_checks : null

  let last
  if (ping) {
    const loss = day?.packet_loss_percent
    last = {
      label: 'Packet loss · 24h',
      value: loss == null ? blank : `${Math.round(loss * 100) / 100}%`,
      note: latest?.ping ? `Latest: ${packets(latest.ping)}` : ' ',
      tone:
        loss == null
          ? 'paused'
          : loss === 0
            ? 'up'
            : s.packet_loss_threshold_percent && loss >= s.packet_loss_threshold_percent
              ? 'down'
              : 'pending',
      icon: TILE_ICONS.loss,
    }
  } else if (dns) {
    const d = latest?.dns
    const odd = d ? oddAnswers(d).size : 0
    last = {
      label: 'Resolvers agreeing',
      value: d ? `${d.answers.length - odd} of ${d.answers.length}` : blank,
      note: !d ? ' ' : d.consistent ? 'Same answer everywhere' : 'A change may be propagating',
      tone: !d ? 'paused' : odd ? 'pending' : 'up',
      icon: TILE_ICONS.resolvers,
    }
  } else {
    const days = s.ssl_expires_at ? sslDaysLeft(s.ssl_expires_at) : null
    last = {
      label: 'SSL certificate',
      value:
        days == null
          ? s.url.startsWith('https:')
            ? blank
            : 'None'
          : days < 0
            ? 'Expired'
            : `${days} ${days === 1 ? 'day' : 'days'}`,
      note:
        days == null
          ? s.url.startsWith('https:')
            ? 'Not read yet'
            : 'Plain HTTP'
          : `${days < 0 ? 'Expired' : 'Expires'} ${shortDate(s.ssl_expires_at)}`,
      tone: days == null ? 'paused' : days <= 7 ? 'down' : days <= 30 ? 'pending' : 'up',
      icon: TILE_ICONS.ssl,
    }
  }

  return (
    <div className="site-stats">
      <StatTile
        label="Uptime · 24h"
        value={day ? percent(day.uptime_percent) : blank}
        note={month?.uptime_percent != null ? `30 days: ${percent(month.uptime_percent)}` : ' '}
        tone={uptimeTone(day?.uptime_percent)}
        icon={TILE_ICONS.uptime}
      />
      <StatTile
        label={ping ? 'Round trip · latest' : dns ? 'Answer time · latest' : 'Response · latest'}
        value={
          !latest ? blank : time == null ? '—' : ping ? rtt(time) : `${time.toLocaleString()} ms`
        }
        note={
          day?.avg_response_ms != null
            ? `24h average ${day.avg_response_ms.toLocaleString()} ms`
            : ' '
        }
        tone={!latest ? 'paused' : !latest.is_up ? 'down' : slow ? 'pending' : 'info'}
        icon={TILE_ICONS.speed}
      />
      <StatTile
        label="Failed checks · 24h"
        value={failed == null ? blank : failed.toLocaleString()}
        note={
          !day ? ' ' : day.checks ? `of ${day.checks.toLocaleString()} checks` : 'No checks yet'
        }
        tone={!day?.checks ? 'paused' : failed ? 'down' : 'up'}
        icon={TILE_ICONS.failed}
      />
      <StatTile {...last} />
    </div>
  )
}

export default function WebsiteDetail() {
  const { id } = useParams()
  const { user } = useAuth()
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const [adding, setAdding] = useState([])
  const [notice, setNotice] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [busy, setBusy] = useState(false)

  const site = useApi(() => api.getWebsite(id), [id], { pollMs: 30000 })
  const checks = useApi(() => api.listChecks(id, CHECKS_SHOWN), [id], { pollMs: 30000 })
  // For the tiles; the rollup behind them moves once a minute.
  const day = useApi(() => api.getWebsiteStats(id, '24h'), [id], { pollMs: 60000 })
  const month = useApi(() => api.getWebsiteStats(id, '30d'), [id], { pollMs: 60000 })
  const projectId = site.data?.project_id
  // A site recipient may see the site without being able to see its project.
  const project = useApi(
    () => (projectId ? api.getProject(projectId).catch(() => null) : null),
    [projectId],
  )
  const canManage = canManageProject(user, project.data)
  const users = useApi(() => (canManage ? api.listUsers({ is_active: true }) : null), [canManage])

  if (site.loading) return <Loading />
  if (!site.data) {
    return (
      <>
        <ErrorBanner error={site.error} />
        <Link to="/">← Back to websites</Link>
      </>
    )
  }

  const s = site.data
  const ping = isPing(s)
  const dns = isDns(s)

  async function run(action) {
    setBusy(true)
    setActionError(null)
    setNotice(null)
    try {
      await action()
    } catch (err) {
      setActionError(err)
    } finally {
      setBusy(false)
    }
  }

  const checkNow = () =>
    run(async () => {
      const result = await api.checkNow(s.id)
      site.setData(result.website)
      checks.reload()
      const c = result.check
      const detail = !c.is_up
        ? `down · ${c.error ?? `HTTP ${c.status_code}`}`
        : c.ping
          ? `up · ${packets(c.ping)}, ${rtt(c.ping.avg_ms)} average round trip`
          : c.dns
            ? `up · ${
                c.dns.records
                  ? `${c.dns.record_type} ${recordsText(c.dns.record_type, c.dns.records)}`
                  : 'resolvers disagree'
              }, ${c.response_time_ms} ms average answer`
            : `up · HTTP ${c.status_code} in ${c.response_time_ms} ms`
      const outcome = result.website.maintenance
        ? ' · in maintenance, so no alerts and no change of status'
        : result.alert_sent
          ? ` · "${result.alert_sent}" alert sent`
          : ''
      setNotice(`Checked just now: ${detail}${outcome}`)
    })

  const toggleEnabled = () =>
    run(async () => site.setData(await api.updateWebsite(s.id, { is_enabled: !s.is_enabled })))

  const remove = () => {
    if (!window.confirm(`Stop monitoring "${s.name}" and delete its check history?`)) return
    run(async () => {
      await api.deleteWebsite(s.id)
      navigate(project.data ? `/projects/${project.data.id}` : '/')
    })
  }

  const removeRecipient = (person) =>
    run(async () => site.setData(await api.removeRecipient(s.id, person.id)))

  const addRecipients = () =>
    run(async () => {
      site.setData(await api.addRecipients(s.id, adding))
      setAdding([])
    })

  const recipientIds = new Set(s.recipients.map((r) => r.id))
  const candidates = (users.data?.items ?? []).filter((u) => !recipientIds.has(u.id))

  return (
    <>
      <p className="crumbs">
        <Link to="/">Websites</Link>
        {project.data && (
          <>
            {' / '}
            <Link to={`/projects/${project.data.id}`}>{project.data.name}</Link>
          </>
        )}
      </p>
      <SiteHero site={s} checks={checks.data ?? []}>
        {canManage && (
          <>
            <button type="button" className="btn btn-primary" onClick={checkNow} disabled={busy}>
              {busy ? 'Working…' : 'Check now'}
            </button>
            <button type="button" className="btn" onClick={toggleEnabled} disabled={busy}>
              {s.is_enabled ? 'Pause' : 'Resume'}
            </button>
            <button type="button" className="btn" onClick={() => setEditing(!editing)}>
              {editing ? 'Close editor' : 'Edit'}
            </button>
          </>
        )}
      </SiteHero>

      <ErrorBanner error={actionError ?? site.error} />
      {notice && <div className="banner banner-info">{notice}</div>}

      {editing && (
        <section className="card">
          <h2>{ping ? 'Edit host' : dns ? 'Edit DNS check' : 'Edit website'}</h2>
          <WebsiteForm
            initial={s}
            project={project.data}
            onCancel={() => setEditing(false)}
            onSubmit={async (payload) => {
              site.setData(await api.updateWebsite(s.id, payload))
              setEditing(false)
            }}
          />
        </section>
      )}

      <div className="site-body">
        <SiteStats site={s} latest={checks.data?.[0]} day={day.data} month={month.data} />

        <div className="site-layout">
          <div className="site-main">
            {dns && !checks.loading && <Resolvers check={checks.data?.[0]} />}

            <SiteHistory websiteId={s.id} ping={ping} dns={dns} />

            <section className="card">
              <h2>Recent checks</h2>
              <ErrorBanner error={checks.error} />
              {checks.loading ? <Loading /> : <Checks checks={checks.data ?? []} site={s} />}
            </section>
          </div>

          <aside className="site-side">
            <Maintenance
              site={s}
              canManage={canManage}
              onChange={site.setData}
              onBoundary={site.reload}
            />

            <div className="site-side-pair">
              <section className="card">
                <h2>Configuration</h2>
                <dl className="kv">
                  <div>
                    <dt>Interval</dt>
                    <dd>every {duration(s.check_interval_seconds)}</dd>
                  </div>
                  {ping ? (
                    <>
                      <div>
                        <dt>Check</dt>
                        <dd>
                          ICMP ping, {s.ping_count} {s.ping_count === 1 ? 'ping' : 'pings'}
                        </dd>
                      </div>
                      <div>
                        <dt>Reply timeout</dt>
                        <dd>{s.timeout_seconds}s</dd>
                      </div>
                      <div>
                        <dt>Packet loss alert at</dt>
                        <dd>
                          {s.packet_loss_threshold_percent
                            ? `${s.packet_loss_threshold_percent}%`
                            : 'server default'}
                        </dd>
                      </div>
                    </>
                  ) : dns ? (
                    <>
                      <div>
                        <dt>Check</dt>
                        <dd>{s.dns_record_type} record lookup</dd>
                      </div>
                      <div>
                        <dt>Resolver timeout</dt>
                        <dd>{s.timeout_seconds}s</dd>
                      </div>
                      <div>
                        <dt>Expected</dt>
                        <dd>
                          {s.dns_expected_values.length
                            ? recordsText(s.dns_record_type, s.dns_expected_values)
                            : 'nothing pinned: alerts when the records change'}
                        </dd>
                      </div>
                      <div>
                        <dt>Last agreed</dt>
                        <dd className="truncate">
                          {s.dns_records
                            ? recordsText(s.dns_record_type, s.dns_records)
                            : 'not learned yet'}
                        </dd>
                      </div>
                    </>
                  ) : (
                    <>
                      <div>
                        <dt>Request</dt>
                        <dd>
                          {s.method}, expects {s.expected_status}
                        </dd>
                      </div>
                      <div>
                        <dt>Timeout</dt>
                        <dd>{s.timeout_seconds}s</dd>
                      </div>
                    </>
                  )}
                  <div>
                    <dt>Alerts per outage</dt>
                    <dd>{s.max_down_alerts}</dd>
                  </div>
                  <div>
                    <dt>Retries</dt>
                    <dd>{s.retries_on_failure}</dd>
                  </div>
                  {s.must_contain && (
                    <div>
                      <dt>Must contain</dt>
                      <dd className="truncate">{s.must_contain}</dd>
                    </div>
                  )}
                  {s.must_not_contain && (
                    <div>
                      <dt>Must not contain</dt>
                      <dd className="truncate">{s.must_not_contain}</dd>
                    </div>
                  )}
                  <div>
                    <dt>
                      {ping
                        ? 'Latency alert above'
                        : dns
                          ? 'Slow answer alert above'
                          : 'Slow after'}
                    </dt>
                    <dd>{s.slow_threshold_ms ? `${s.slow_threshold_ms} ms` : 'server default'}</dd>
                  </div>
                  <div>
                    <dt>Monitoring since</dt>
                    <dd>{dateTime(s.created_at)}</dd>
                  </div>
                </dl>
              </section>

              <section className="card">
                <h2>Alerting</h2>
                <dl className="kv">
                  <div>
                    <dt>Channels</dt>
                    <dd>
                      {s.alert_channels.length
                        ? s.alert_channels.map((c) => (
                            <span key={c} className="chip">
                              {c}
                            </span>
                          ))
                        : '—'}
                    </dd>
                  </div>
                  <div>
                    <dt>Project recipients</dt>
                    <dd>{s.inherit_project_recipients ? 'included' : 'not included'}</dd>
                  </div>
                  <div>
                    <dt>Extra emails</dt>
                    <dd>{s.alert_emails.length ? s.alert_emails.join(', ') : '—'}</dd>
                  </div>
                  {(s.slack_channel_id || s.alert_channels.includes('slack')) && (
                    <div>
                      <dt>Slack</dt>
                      <dd>
                        {s.slack_channel_id ?? "project's channel"} ·{' '}
                        {s.slack_token_hint ? `own bot (${s.slack_token_hint})` : "project's bot"}
                      </dd>
                    </div>
                  )}
                  {(s.telegram_chat_id || s.alert_channels.includes('telegram')) && (
                    <div>
                      <dt>Telegram</dt>
                      <dd>
                        {s.telegram_chat_id ?? "project's chat"} ·{' '}
                        {s.telegram_token_hint
                          ? `own bot (${s.telegram_token_hint})`
                          : "project's bot"}
                      </dd>
                    </div>
                  )}
                  {(s.whatsapp_recipients.length > 0 || s.alert_channels.includes('whatsapp')) && (
                    <div>
                      <dt>WhatsApp</dt>
                      <dd>
                        {s.whatsapp_recipients.length
                          ? s.whatsapp_recipients.join(', ')
                          : "project's numbers"}
                      </dd>
                    </div>
                  )}
                </dl>
                <h3>Site recipients</h3>
                <p className="muted small">
                  Recipients can see this site and its checks, even if they aren&apos;t in its
                  project.
                </p>
                <PersonList
                  people={s.recipients}
                  onRemove={canManage ? removeRecipient : null}
                  emptyLabel="No users are alerted about this site specifically."
                />
                {canManage && candidates.length > 0 && (
                  <details className="advanced">
                    <summary>Add recipients</summary>
                    <UserChecklist users={candidates} selected={adding} onChange={setAdding} />
                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={addRecipients}
                      disabled={busy || !adding.length}
                    >
                      Add {adding.length || ''} selected
                    </button>
                  </details>
                )}
              </section>
            </div>

            {canManage && (
              <section className="card card-danger">
                <h2>Delete {ping ? 'host' : dns ? 'DNS check' : 'website'}</h2>
                <p className="muted small">
                  Stops monitoring {s.name} and deletes its check history. This can&apos;t be
                  undone.
                </p>
                <button type="button" className="btn btn-danger" onClick={remove} disabled={busy}>
                  Delete
                </button>
              </section>
            )}
          </aside>
        </div>
      </div>
    </>
  )
}
