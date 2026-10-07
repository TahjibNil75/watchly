import { Fragment, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { dbEngine, dbSaid, isDatabase, isDns, isPing, recordText, rtt } from '../checkTypes.js'
import {
  CheckTypeBadge,
  ConfirmDialog,
  Empty,
  EnvironmentBadge,
  ErrorBanner,
  Loading,
  PersonList,
  StatusBadge,
  UserChecklist,
} from '../components.jsx'
import { bytes, dateTime, duration, percent, since, timeAgo, until } from '../format.js'
import Maintenance from '../Maintenance.jsx'
import { canManageProject } from '../roles.js'
import SiteHistory from '../SiteHistory.jsx'
import { useApi } from '../useApi.js'
import WebsiteForm from '../WebsiteForm.jsx'

const CHECKS_SHOWN = 60

// A failed connection has no response, so its recorded time (often 0) means
// nothing. A ping's time is its average round trip, when anything came back,
// a DNS check's the resolvers' average answer time, and a database check's the
// time to the server's answer, when it gave one.
const responseTime = (c) =>
  c.ping
    ? c.ping.avg_ms
    : c.dns || c.status_code != null || (c.database && c.is_up)
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

// What the Details panel can show of an HTTP check; older rows have little or none.
const hasDetails = (c) =>
  STEPS.some(([key]) => c[key] != null) ||
  c.redirects?.length > 0 ||
  c.content_length != null ||
  c.ip_address != null ||
  Object.keys(c.headers ?? {}).length > 0

// One check's time as a bar split by step, with whatever is left over
// (reading the body, waiting on the server past the first byte) as the rest.
function TimingBar({ check: c }) {
  const steps = STEPS.filter(([key]) => c[key] != null)
  if (!steps.length) return null
  const used = steps.reduce((sum, [key]) => sum + c[key], 0)
  const total = Math.max(c.response_time_ms ?? 0, used, 1)
  const rest = total - used
  const parts = [...steps.map(([key, label]) => [key, label, c[key]]), ['rest', 'other', rest]]
  return (
    <div>
      <div className="timing-bar" role="img" aria-label={timeSplit(c)}>
        {parts
          .filter(([, , ms]) => ms > 0)
          .map(([key, label, ms]) => (
            <span
              key={key}
              className={`timing-seg timing-${key}`}
              style={{ flexGrow: ms }}
              title={`${label} ${ms} ms`}
            />
          ))}
      </div>
      <ul className="timing-legend">
        {parts
          .filter(([key, , ms]) => key !== 'rest' || ms > 0)
          .map(([key, label, ms]) => (
            <li key={key}>
              <span className={`timing-swatch timing-${key}`} /> {label} {ms} ms
            </li>
          ))}
      </ul>
      {c.redirects?.length > 0 && (
        <p className="muted small">Steps are summed over every redirect hop.</p>
      )}
    </div>
  )
}

// The network side of one HTTP check, the way a browser's Network tab would
// list it for the first request: timings, redirects, size and headers.
function CheckDetails({ check: c }) {
  const headers = Object.entries(c.headers ?? {})
  return (
    <div className="check-details">
      <TimingBar check={c} />
      {c.redirects?.length > 0 && (
        <div>
          <h3>Redirects</h3>
          <ol className="redirects">
            {c.redirects.map((hop, i) => (
              <li key={i}>
                <span className="badge badge-unknown">{hop.status}</span>
                <span className="truncate" title={hop.url}>
                  {hop.url}
                </span>
                <span className="muted">→</span>
                <span className="truncate" title={hop.location ?? undefined}>
                  {hop.location ?? '—'}
                </span>
              </li>
            ))}
            <li>
              <span className={`badge badge-${c.is_up ? 'up' : 'down'}`}>{c.status_code ?? '—'}</span>
              <span className="truncate" title={c.final_url ?? undefined}>
                {c.final_url}
              </span>
            </li>
          </ol>
        </div>
      )}
      {(c.content_length != null || c.ip_address != null || headers.length > 0) && (
        <dl className="kv">
          {c.ip_address != null && (
            <div>
              <dt>Server address</dt>
              <dd>
                <code>{c.ip_address}</code>
              </dd>
            </div>
          )}
          {c.content_length != null && (
            <div>
              <dt>Response size</dt>
              <dd>{bytes(c.content_length)}</dd>
            </div>
          )}
          {headers.map(([name, value]) => (
            <div key={name}>
              <dt>{name}</dt>
              <dd>
                <code>{value}</code>
              </dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  )
}

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

// The host of a database check's `host:port`, an IPv6 address out of its brackets.
const endpointHost = (url) => url.replace(/:\d+$/, '').replace(/^\[|\]$/g, '')

// "MariaDB 11.4.2", "PostgreSQL": what the server says it is.
const dbName = (d) => {
  const name = d.product ?? dbEngine(d.engine).label.split(' / ')[0]
  return d.version ? `${name} ${d.version}` : name
}

// "DNS 2 ms · connect 3 ms · TLS 8 ms · answer 3 ms".
const dbTimeSplit = (c) => timeSplit(c).replace('first byte', 'answer')

// A database check's row: what the server said instead of an HTTP status.
function DatabaseCells({ check: c, host }) {
  const d = c.database
  const said = c.is_up ? dbSaid(d) : d?.message
  return (
    <>
      <td title={d?.code ? `Server code ${d.code}` : undefined}>
        <div className="truncate">{said ?? '—'}</div>
      </td>
      <td className="nowrap" title={dbTimeSplit(c) || undefined}>
        {responseTime(c) != null ? `${responseTime(c)} ms` : '—'}
      </td>
      <td className="muted small">
        {c.error ?? ''}
        {/* What a host name resolved to. */}
        {c.ip_address && c.ip_address !== host && (
          <div className="truncate">→ {c.ip_address}</div>
        )}
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
  const [openId, setOpenId] = useState(null)
  if (!checks.length) return <Empty>No checks yet. The first one runs within a minute.</Empty>
  const ping = isPing(site)
  const dns = isDns(site)
  const database = isDatabase(site)
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
                <th>{ping ? 'Packets' : dns ? 'Records' : database ? 'Server said' : 'HTTP'}</th>
                <th>{ping ? 'Round trip' : dns ? 'Answer time' : 'Response'}</th>
                <th>Error</th>
                {!ping && !dns && !database && <th aria-label="Details" />}
              </tr>
            </thead>
            <tbody>
              {(expanded ? shown : shown.slice(0, COLLAPSED_ROWS)).map((c) => (
                <Fragment key={c.id}>
                  <tr>
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
                    ) : database ? (
                      <DatabaseCells check={c} host={endpointHost(site.url)} />
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
                        <td>
                          {hasDetails(c) && (
                            <button
                              type="button"
                              className="btn btn-sm"
                              aria-expanded={openId === c.id}
                              onClick={() => setOpenId(openId === c.id ? null : c.id)}
                            >
                              {openId === c.id ? 'Hide' : 'Details'}
                            </button>
                          )}
                        </td>
                      </>
                    )}
                  </tr>
                  {openId === c.id && (
                    <tr className="check-details-row">
                      <td colSpan={6}>
                        <CheckDetails check={c} />
                      </td>
                    </tr>
                  )}
                </Fragment>
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

// What the database server said at the latest check, and how the session went.
function DatabaseServer({ site: s, check }) {
  const d = check?.database
  if (!d) return null
  const engine = dbEngine(s.db_engine)
  const rows = [
    ['Server', dbName(d)],
    d.role && ['Role', d.replica_set ? `${d.role} of replica set ${d.replica_set}` : d.role],
    [check.is_up ? 'It said' : 'Error', check.is_up ? dbSaid(d) ?? '—' : check.error],
    !check.is_up && d.message && ['It said', d.message],
    d.code && ['Server code', <code key="code">{d.code}</code>],
    [
      'TLS',
      d.tls == null
        ? 'not reached'
        : d.tls
          ? d.tls_version ?? 'yes'
          : engine.tlsFromStart
            ? 'off for this check'
            : 'not offered by the server',
    ],
    check.ip_address && ['Address', <code key="ip">{check.ip_address}</code>],
    dbTimeSplit(check) && ['Timing', dbTimeSplit(check)],
  ].filter(Boolean)
  return (
    <section className="card">
      <h2>Database server</h2>
      <p className="muted small">
        What it said at the latest check, {timeAgo(check.checked_at)}. Watchly goes as far as the
        server&apos;s first answer and never logs in.
      </p>
      <dl className="kv">
        {rows.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </section>
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

// Whole days until a certificate or domain ends; negative once it has.
const daysLeft = (iso) => Math.floor((new Date(iso).getTime() - Date.now()) / 86_400_000)

const shortDate = (iso) =>
  new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })

// Red inside a week, amber inside a month, as the SSL tile has it.
const expiryTone = (days) => (days <= 7 ? 'down' : days <= 30 ? 'pending' : 'up')

const expiryText = (days) =>
  days < 0 ? 'Expired' : days === 0 ? 'Less than a day left' : `${days} ${days === 1 ? 'day' : 'days'} left`

// "58 days left · 29 Nov 2026", the first part in its tone's colour.
function Expiry({ iso }) {
  const days = daysLeft(iso)
  return (
    <>
      <span className={`text-${expiryTone(days)}`}>{expiryText(days)}</span> ·{' '}
      <span className="nowrap" title={dateTime(iso)}>
        {shortDate(iso)}
      </span>
    </>
  )
}

// The host a site's URL names; for a ping or DNS check the URL is the host.
function hostOf(site) {
  if (isPing(site) || isDns(site)) return site.url.toLowerCase()
  try {
    return new URL(site.url).hostname
  } catch {
    return ''
  }
}

// An IP address, or a name without a dot, has no domain to look up.
const hasDomain = (host) => host.includes('.') && !/^[\d.]+$/.test(host) && !host.includes(':')

// Whether a certificate name, `*.example.com` included, covers `host`. A
// wildcard stands for exactly one label.
function certCovers(name, host) {
  const n = name.toLowerCase()
  if (!n.startsWith('*.')) return n === host
  const rest = n.slice(1)
  const label = host.slice(0, -rest.length)
  return host.endsWith(rest) && label.length > 0 && !label.includes('.')
}

const SANS_SHOWN = 6

function CertNames({ names, host }) {
  if (!names.length) return '—'
  const chip = (n) => (
    <span key={n} className={`chip${certCovers(n, host) ? ' chip-match' : ''}`}>
      {n}
    </span>
  )
  return (
    <div className="cert-names">
      {names.slice(0, SANS_SHOWN).map(chip)}
      {names.length > SANS_SHOWN && (
        <details className="advanced">
          <summary>{names.length - SANS_SHOWN} more</summary>
          {names.slice(SANS_SHOWN).map(chip)}
        </details>
      )}
    </div>
  )
}

function CertificatePart({ site: s }) {
  const host = hostOf(s)
  const names = s.ssl_sans ?? []
  const covered = names.length ? names.some((n) => certCovers(n, host)) : null
  return (
    <div className="site-registry-part">
      <h2>SSL certificate</h2>
      {!s.ssl_expires_at ? (
        <p className="muted small">
          {s.ssl_checked_at
            ? `The certificate could not be read ${timeAgo(s.ssl_checked_at)}. It is tried again every few hours.`
            : 'Read with the next check.'}
        </p>
      ) : (
        <dl className="kv">
          <div>
            <dt>Expires</dt>
            <dd>
              <Expiry iso={s.ssl_expires_at} />
            </dd>
          </div>
          <div>
            <dt>Issued to</dt>
            <dd>{s.ssl_subject ?? '—'}</dd>
          </div>
          <div>
            <dt>Issuer</dt>
            <dd>{s.ssl_issuer ?? '—'}</dd>
          </div>
          <div>
            <dt>Valid from</dt>
            <dd>{s.ssl_valid_from ? shortDate(s.ssl_valid_from) : '—'}</dd>
          </div>
          <div>
            <dt>TLS version</dt>
            <dd>{s.ssl_tls_version ? s.ssl_tls_version.replace('TLSv', 'TLS ') : '—'}</dd>
          </div>
          {s.ssl_cipher && (
            <div>
              <dt>Cipher</dt>
              <dd>{s.ssl_cipher}</dd>
            </div>
          )}
          {s.ssl_alpn && (
            <div>
              <dt>HTTP/2</dt>
              <dd>{s.ssl_alpn === 'h2' ? 'Supported' : 'Not offered (HTTP/1.1 only)'}</dd>
            </div>
          )}
          <div className="kv-stack">
            <dt>Covers</dt>
            <dd>
              <CertNames names={names} host={host} />
              {covered === false && <div className="text-down small">Does not cover {host}</div>}
            </dd>
          </div>
          {s.ssl_chain?.length > 0 && (
            <div className="kv-stack">
              <dt>Chain sent</dt>
              <dd>
                <ol className="cert-chain">
                  {s.ssl_chain.map((c, i) => (
                    <li key={i}>
                      <strong>{c.subject ?? '—'}</strong>
                      <span className="muted small">
                        {' '}
                        issued by {c.issuer ?? '—'} · expires {shortDate(c.expires_at)}
                      </span>
                      {i > 0 && c.expires_at < s.ssl_chain[0].expires_at && (
                        <div className="text-down small">
                          Expires before the site's own certificate
                        </div>
                      )}
                    </li>
                  ))}
                </ol>
              </dd>
            </div>
          )}
          <div>
            <dt>Last read</dt>
            <dd title={dateTime(s.ssl_checked_at)}>{timeAgo(s.ssl_checked_at)}</dd>
          </div>
        </dl>
      )}
    </div>
  )
}

// A nameserver change is flagged on the page for a week after it is seen.
const recentlyChanged = (iso) => iso && Date.now() - new Date(iso).getTime() < 7 * 86_400_000

function DomainPart({ site: s }) {
  return (
    <div className="site-registry-part">
      <h2>Domain</h2>
      {!s.domain_checked_at ? (
        <p className="muted small">Looked up with the next check.</p>
      ) : (
        <dl className="kv">
          <div>
            <dt>Domain</dt>
            <dd>{s.domain_name ?? '—'}</dd>
          </div>
          <div>
            <dt>Expires</dt>
            <dd>{s.domain_expires_at ? <Expiry iso={s.domain_expires_at} /> : '—'}</dd>
          </div>
          <div>
            <dt>Registrar</dt>
            <dd>{s.domain_registrar ?? '—'}</dd>
          </div>
          <div className="kv-stack">
            <dt>
              Nameservers
              {recentlyChanged(s.domain_nameservers_changed_at) && (
                <span
                  className="text-pending ns-changed"
                  title={dateTime(s.domain_nameservers_changed_at)}
                >
                  changed {timeAgo(s.domain_nameservers_changed_at)}
                </span>
              )}
            </dt>
            <dd>
              {s.domain_nameservers?.length ? (
                <div className="ns-list">
                  {s.domain_nameservers.map((n) => (
                    <code key={n}>{n}</code>
                  ))}
                </div>
              ) : (
                <span className="muted">The registry does not list them.</span>
              )}
            </dd>
          </div>
          <div>
            <dt>Last looked up</dt>
            <dd title={dateTime(s.domain_checked_at)}>{timeAgo(s.domain_checked_at)}</dd>
          </div>
        </dl>
      )}
      {s.domain_error && <p className="muted small domain-error">{s.domain_error}</p>}
    </div>
  )
}

// A grade in the tiles' colours: all or all but one header ok is good.
const gradeTone = (grade) => ({ A: 'up', B: 'up', C: 'unknown', D: 'unknown' })[grade] ?? 'down'

const HEADER_STATUS = {
  ok: { tone: 'up', label: 'OK' },
  weak: { tone: 'unknown', label: 'Weak' },
  missing: { tone: 'down', label: 'Missing' },
}

// "HIT", "MISS"...: whether a cache answered, in the tiles' colours.
const cacheTone = (cached) => (cached === true ? 'up' : cached === false ? 'unknown' : 'paused')

// Whether the site is served through a CDN, and the traces that say so.
function CdnCard({ site: s }) {
  const cdn = s.cdn
  return (
    <section className="card">
      <div className="card-head">
        <h2>CDN</h2>
        {cdn && (
          <span className={`badge badge-${cdn.providers.length ? 'up' : 'paused'}`}>
            {cdn.providers.length
              ? cdn.providers.map((p) => p.name).join(' + ')
              : cdn.unidentified_cache
                ? 'Cache or proxy'
                : 'None detected'}
          </span>
        )}
      </div>
      {!cdn ? (
        <p className="muted small">Read with the next check that comes up.</p>
      ) : (
        <>
          {cdn.providers.length > 0 ? (
            <ul className="cdn-list">
              {cdn.providers.map((p) => (
                <li key={p.name}>
                  <strong>{p.name}</strong>
                  <ul>
                    {p.evidence.map((e) => (
                      <li key={e}>
                        <code>{e}</code>
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          ) : cdn.unidentified_cache ? (
            <p className="small">
              A cache or proxy answered (it sent <code>via</code>, <code>x-cache</code> or{' '}
              <code>age</code>), but nothing names a known CDN.
            </p>
          ) : (
            <p className="small">
              No CDN found. The response carries none of the known CDN headers and{' '}
              <code>{cdn.host}</code> is not an alias of a CDN's name. A CDN that hides both
              would look the same.
            </p>
          )}
          <dl className="kv">
            {cdn.cache_status && (
              <div>
                <dt>Cache</dt>
                <dd>
                  <span className={`badge badge-${cacheTone(cdn.cached)}`}>{cdn.cache_status}</span>
                  {cdn.age_seconds != null && (
                    <span className="muted small"> · cached {duration(cdn.age_seconds)} ago</span>
                  )}
                </dd>
              </div>
            )}
            {cdn.cname_chain.length > 0 && (
              <div className="kv-stack">
                <dt>DNS aliases of {cdn.host}</dt>
                <dd>
                  <code>{cdn.cname_chain.join(' → ')}</code>
                </dd>
              </div>
            )}
          </dl>
          <p className="muted small sec-foot">
            Last read <span title={dateTime(s.cdn_checked_at)}>{timeAgo(s.cdn_checked_at)}</span>.
          </p>
        </>
      )}
    </section>
  )
}

// "United States (US)"; the bare code where the browser cannot name it.
function countryName(code) {
  try {
    const name = new Intl.DisplayNames(undefined, { type: 'region' }).of(code)
    return name && name !== code ? `${name} (${code})` : code
  } catch {
    return code
  }
}

// What an HTTP site is served from: the address it answered on, whose network
// that is and what the address is called.
function ServerCard({ site: s }) {
  const server = s.server
  const h2 = s.ssl_alpn === 'h2'
  const protocols = [h2 && 'HTTP/2', server?.h3 && 'HTTP/3'].filter(Boolean)
  const others = (server?.ips_seen ?? []).filter((ip) => ip !== server.ip)
  return (
    <section className="card">
      <div className="card-head">
        <h2>Server</h2>
        {server && <span className="badge badge-paused">IPv{server.version}</span>}
      </div>
      {!server ? (
        <p className="muted small">Read with the next check that comes up.</p>
      ) : (
        <>
          <dl className="kv">
            <div>
              <dt>Address</dt>
              <dd>
                <code>{server.ip}</code>
              </dd>
            </div>
            {server.ptr && (
              <div>
                <dt>Reverse DNS</dt>
                <dd className="truncate" title={server.ptr}>
                  {server.ptr}
                </dd>
              </div>
            )}
            {server.asn != null && (
              <div>
                <dt>Network</dt>
                <dd>
                  AS{server.asn}
                  {server.as_name && <span className="muted"> · {server.as_name}</span>}
                </dd>
              </div>
            )}
            {server.prefix && (
              <div>
                <dt>Range</dt>
                <dd>
                  <code>{server.prefix}</code>
                </dd>
              </div>
            )}
            {server.country && (
              <div>
                <dt>Registered in</dt>
                <dd>
                  {countryName(server.country)}
                  {server.registry && (
                    <span className="muted small"> · {server.registry.toUpperCase()}</span>
                  )}
                </dd>
              </div>
            )}
            <div>
              <dt>Protocols</dt>
              <dd>
                {protocols.length ? (
                  protocols.map((p) => (
                    <span key={p} className="chip">
                      {p}
                    </span>
                  ))
                ) : (
                  <span className="muted">HTTP/1.1 only</span>
                )}
              </dd>
            </div>
            {others.length > 0 && (
              <div className="kv-stack">
                <dt>Also answered on</dt>
                <dd>
                  <div className="ns-list">
                    {others.map((ip) => (
                      <code key={ip}>{ip}</code>
                    ))}
                  </div>
                </dd>
              </div>
            )}
          </dl>
          <p className="muted small sec-foot">
            The country is where the address block is registered, which a CDN or anycast address
            can answer from elsewhere. Last read{' '}
            <span title={dateTime(s.server_checked_at)}>{timeAgo(s.server_checked_at)}</span>.
          </p>
        </>
      )}
    </section>
  )
}

// How the last successful response's security headers grade, one row each.
function SecurityHeaders({ site: s }) {
  const report = s.security
  return (
    <section className="card">
      <div className="card-head">
        <h2>Security headers</h2>
        {report && (
          <span className={`badge badge-${gradeTone(report.grade)} sec-grade`}>
            {report.grade} · {report.score} of {report.total}
          </span>
        )}
      </div>
      {!report ? (
        <p className="muted small">Read from the next check that comes up.</p>
      ) : (
        <>
          <ul className="sec-list">
            {report.items.map((item) => {
              const status = HEADER_STATUS[item.status]
              return (
                <li key={item.key}>
                  <span className={`badge badge-${status.tone}`}>{status.label}</span>
                  <div className="sec-body">
                    <strong>{item.header}</strong>
                    {item.value && (
                      <code className="sec-value" title={item.value}>
                        {item.value}
                      </code>
                    )}
                    {item.note && <span className="muted small">{item.note}</span>}
                  </div>
                </li>
              )
            })}
          </ul>
          {report.extras?.length > 0 && (
            <>
              <h3 className="sec-extras-title">Also sent, not graded</h3>
              <ul className="sec-list">
                {report.extras.map((x) => (
                  <li key={x.header}>
                    <div className="sec-body">
                      <strong>{x.header}</strong>
                      <code className="sec-value" title={x.value}>
                        {x.value}
                      </code>
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
          <p className="muted small sec-foot">
            From {report.url},{' '}
            <span title={dateTime(s.security_checked_at)}>{timeAgo(s.security_checked_at)}</span>.
          </p>
        </>
      )}
    </section>
  )
}

// The certificate and the domain, whichever apply to the site, as one card:
// side by side when both do, so their different heights need no filling.
function Registration({ site: s }) {
  const cert = !isPing(s) && !isDns(s) && s.url.startsWith('https:')
  const domain = hasDomain(hostOf(s))
  if (!cert && !domain) return null
  return (
    <section className={`card site-registry${cert && domain ? '' : ' is-single'}`}>
      {cert && <CertificatePart site={s} />}
      {domain && <DomainPart site={s} />}
    </section>
  )
}

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
  maintenance: (
    <>
      <path d="M10 3.5h4l3.6 13.5H6.4zM8.1 10.5h7.8" />
      <rect x="3.5" y="17" width="17" height="3.5" rx="1" />
    </>
  ),
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
  database: (
    <>
      <ellipse cx="12" cy="5.5" rx="7.5" ry="2.5" />
      <path d="M4.5 5.5v13c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5v-13M4.5 12c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5" />
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
            ) : isDatabase(s) ? (
              <>
                <code>{s.url}</code> · {dbEngine(s.db_engine).label}
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
  } else if (isDatabase(s)) {
    const d = latest?.database
    // The name alone fits the tile; the version goes underneath.
    const name = d?.product ?? dbEngine(s.db_engine).label.split(' / ')[0]
    const said = latest?.is_up ? dbSaid(d) : 'Not answering'
    last = {
      label: 'Server',
      value: name,
      note: !latest ? ' ' : [d?.version, said].filter(Boolean).join(' · ') || ' ',
      tone: !latest ? 'paused' : latest.is_up ? 'up' : 'down',
      icon: TILE_ICONS.database,
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
    const days = s.ssl_expires_at ? daysLeft(s.ssl_expires_at) : null
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
      tone: days == null ? 'paused' : expiryTone(days),
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
  const [confirmingDelete, setConfirmingDelete] = useState(false)

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
        <Link to="/websites">← Back to websites</Link>
      </>
    )
  }

  const s = site.data
  const ping = isPing(s)
  const dns = isDns(s)
  const database = isDatabase(s)

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
            : c.database
              ? `up · ${dbName(c.database)} answered in ${c.response_time_ms} ms${
                  dbSaid(c.database) ? `: ${dbSaid(c.database)}` : ''
                }`
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
    run(async () => {
      await api.deleteWebsite(s.id)
      navigate(project.data ? `/projects/${project.data.id}` : '/websites')
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
        <Link to="/websites">Websites</Link>
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
            <button type="button" className="btn btn-purple" onClick={toggleEnabled} disabled={busy}>
              {s.is_enabled ? 'Pause' : 'Resume'}
            </button>
            <button type="button" className="btn btn-warn" onClick={() => setEditing(!editing)}>
              {editing ? 'Close editor' : 'Edit'}
            </button>
          </>
        )}
      </SiteHero>

      <ErrorBanner error={actionError ?? site.error} />
      {notice && <div className="banner banner-info">{notice}</div>}

      {editing && (
        <section className="card">
          <h2>
            {ping
              ? 'Edit host'
              : dns
                ? 'Edit DNS check'
                : database
                  ? 'Edit database check'
                  : 'Edit website'}
          </h2>
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

        {dns && !checks.loading && <Resolvers check={checks.data?.[0]} />}
        {database && !checks.loading && <DatabaseServer site={s} check={checks.data?.[0]} />}

        <SiteHistory websiteId={s.id} ping={ping} dns={dns} database={database} />

        <Registration site={s} />

        {/* Equal-height rows: the cards in a row share a top and a bottom edge. */}
        {!ping && !dns && !database && (
          <>
            <div className="site-row site-pair">
              <ServerCard site={s} />
              <CdnCard site={s} />
            </div>
            <SecurityHeaders site={s} />
          </>
        )}

        <div className="site-row site-trio">
          <Maintenance
            site={s}
            canManage={canManage}
            onChange={site.setData}
            onBoundary={site.reload}
          />

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
              ) : database ? (
                <>
                  <div>
                    <dt>Check</dt>
                    <dd>{dbEngine(s.db_engine).label} handshake, no login</dd>
                  </div>
                  <div>
                    <dt>TLS</dt>
                    <dd>
                      {dbEngine(s.db_engine).tlsFromStart
                        ? s.db_tls
                          ? 'from the start'
                          : 'off'
                        : 'whenever the server offers it'}
                    </dd>
                  </div>
                  <div>
                    <dt>Timeout per step</dt>
                    <dd>{s.timeout_seconds}s</dd>
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
                  <dt>Contains</dt>
                  <dd className="truncate">{s.must_contain}</dd>
                </div>
              )}
              {s.must_not_contain && (
                <div>
                  <dt>Does not contain</dt>
                  <dd className="truncate">{s.must_not_contain}</dd>
                </div>
              )}
              {s.request_headers?.length > 0 && (
                <div>
                  <dt>Request headers</dt>
                  {/* Names only: the values may be secrets. */}
                  <dd className="truncate">
                    {s.request_headers.map((h) => h.name).join(', ')}
                  </dd>
                </div>
              )}
              <div>
                <dt>
                  {ping
                    ? 'Latency alert above'
                    : dns || database
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

        {/* Full width: the log has room for its error column, and deleting
            comes last. */}
        <section className="card">
          <h2>Recent checks</h2>
          <ErrorBanner error={checks.error} />
          {checks.loading ? <Loading /> : <Checks checks={checks.data ?? []} site={s} />}
        </section>

        {canManage && (
          <section className="card card-danger">
            <h2>Delete {ping ? 'host' : dns ? 'DNS check' : 'website'}</h2>
            <p className="muted small">
              Stops monitoring {s.name} and deletes its check history. This can&apos;t be
              undone.
            </p>
            <button
              type="button"
              className="btn btn-danger-solid"
              onClick={() => setConfirmingDelete(true)}
              disabled={busy}
            >
              Delete
            </button>
          </section>
        )}
        {confirmingDelete && (
          <ConfirmDialog
            title={`Delete ${s.name}?`}
            busy={busy}
            error={actionError}
            onConfirm={remove}
            onCancel={() => setConfirmingDelete(false)}
          >
            Stops monitoring {s.name} and deletes its check history. This can&apos;t be undone.
          </ConfirmDialog>
        )}
      </div>
    </>
  )
}
