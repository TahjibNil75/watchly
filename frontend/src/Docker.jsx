import { useLayoutEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Empty } from './components.jsx'
import {
  AGENT_REPO,
  CONDITIONS,
  HOST_STATUSES,
  PROBLEM_LABELS,
  composeSnippet,
  cpuShare,
  dockerRunSnippet,
  memory,
  pct,
} from './docker.js'
import { dateTime, since, timeAgo, timeFormat } from './format.js'

export function ConditionBadge({ condition }) {
  const c = CONDITIONS[condition] ?? CONDITIONS.unknown
  return <span className={`badge badge-${c.badge}`}>{c.label}</span>
}

export function HostStatusBadge({ status }) {
  const s = HOST_STATUSES[status] ?? HOST_STATUSES.pending
  return <span className={`badge badge-${s.badge}`}>{s.label}</span>
}

// The Docker whale's hull and containers, as bare paths for a 24x24 stroke
// icon: the icon for Docker things.
export function DockerGlyph() {
  return (
    <>
      <path d="M2.5 12.5h17.2c.8 0 1.6-.9 1.8-2 .9.1 1.6-.3 2-1-.6-.5-1.4-.6-2.1-.4-.4-1-1.1-1.6-1.8-1.9-.5.9-.6 2.2-.1 3.3" />
      <path d="M2.5 12.5c.4 4.2 3.6 7 8.5 7 4.3 0 7.6-2.2 9-6.6" />
      <path d="M5 9.5h3v3H5zM8 9.5h3v3H8zM11 9.5h3v3h-3zM8 6.5h3v3H8zM11 6.5h3v3h-3z" />
    </>
  )
}

export function DockerIcon({ className = 'icon' }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <DockerGlyph />
    </svg>
  )
}

// One line of what a container uses: CPU as a share of its host, memory
// against its limit.
export function Usage({ container, cpus }) {
  const m = container.metrics
  if (!m || !['running', 'unhealthy'].includes(container.condition)) return <span className="muted">—</span>
  const share = cpuShare(m.cpu_pct, cpus)
  return (
    <span className="docker-usage">
      <span title="CPU, as a share of the host's cores">CPU {pct(share, share != null && share < 10 ? 1 : 0)}</span>
      <span title={`Memory against its ${memory(m.mem_limit_bytes)} limit`}>
        {memory(m.mem_used_bytes)}
        {m.mem_pct != null && <span className="muted"> · {pct(m.mem_pct)}</span>}
      </span>
    </span>
  )
}

export function ProblemChips({ problems }) {
  const keys = Object.keys(problems ?? {})
  if (!keys.length) return null
  return (
    <span className="docker-problems">
      {keys.map((key) => (
        <span key={key} className="badge badge-degraded" title={problems[key].detail ?? undefined}>
          {PROBLEM_LABELS[key] ?? key.replaceAll('_', ' ')}
        </span>
      ))}
    </span>
  )
}

// `hosts` maps a host id to its read, for its CPU count and a link; on a single
// host's page pass `showHost={false}` to drop the column.
export function ContainerTable({ containers, hosts, showHost = true, emptyLabel }) {
  if (!containers.length) return <Empty>{emptyLabel}</Empty>
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Status</th>
            <th>Container</th>
            {hosts && showHost && <th>Host</th>}
            <th>Uses</th>
            <th>Restarts</th>
            <th>Since</th>
          </tr>
        </thead>
        <tbody>
          {containers.map((c) => (
            <tr key={c.id} className={c.muted ? 'is-muted' : undefined}>
              <td>
                <ConditionBadge condition={c.condition} />
              </td>
              <td>
                <Link to={`/docker/containers/${c.id}`} className="strong-link">
                  {c.name}
                </Link>
                {c.muted && <span className="muted small"> · muted</span>}
                <div className="muted small truncate" title={c.image}>
                  {c.compose_project ? `${c.compose_project} / ${c.compose_service ?? '—'} · ` : ''}
                  {c.image}
                </div>
                <ProblemChips problems={c.problems} />
              </td>
              {hosts && showHost && (
                <td className="nowrap">
                  <Link to={`/docker/hosts/${c.host.id}`}>{c.host.name}</Link>
                </td>
              )}
              <td className="nowrap">
                <Usage container={c} cpus={hosts?.[c.host.id]?.cpus} />
              </td>
              <td className="nowrap">{c.restart_count}</td>
              <td className="nowrap">
                {c.down_since ? (
                  <span className="text-down">down {since(c.down_since)}</span>
                ) : c.condition === 'running' || c.condition === 'unhealthy' ? (
                  c.started_at ? `up ${since(c.started_at)}` : '—'
                ) : c.finished_at ? (
                  <span className="muted">exited {timeAgo(c.finished_at)}</span>
                ) : (
                  <span className="muted">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

const EVENT_TONES = {
  down: 'down',
  host_offline: 'down',
  oom: 'down',
  restart_loop: 'down',
  die: 'down',
  recovered: 'up',
  host_recovered: 'up',
  start: 'up',
  unhealthy: 'pending',
  high_cpu: 'pending',
  high_memory: 'pending',
  health_status: 'pending',
}

export function EventFeed({ events, showHost = false, emptyLabel = 'Nothing has happened yet.' }) {
  if (!events.length) return <p className="muted small">{emptyLabel}</p>
  return (
    <ul className="docker-feed">
      {events.map((e) => (
        <li key={e.id} className={`tone-${EVENT_TONES[e.kind] ?? 'neutral'}`}>
          <span className="docker-feed-dot" aria-hidden="true" />
          <span className="docker-feed-text">
            {e.container_id ? (
              <Link to={`/docker/containers/${e.container_id}`}>{e.summary}</Link>
            ) : (
              e.summary
            )}
            {e.source === 'watchly' && <span className="chip">alert</span>}
            {showHost && <span className="muted small"> · {e.host_name}</span>}
          </span>
          <time className="muted small nowrap" dateTime={e.occurred_at} title={dateTime(e.occurred_at)}>
            {timeAgo(e.occurred_at)}
          </time>
        </li>
      ))}
    </ul>
  )
}

function CopyBlock({ text, label }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1800)
    } catch {
      // No clipboard (plain http): the text is selectable anyway.
    }
  }
  return (
    <div className="docker-snippet">
      <pre aria-label={label}>{text}</pre>
      <button type="button" className="btn btn-sm" onClick={copy}>
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  )
}

// How to run the agent with `token`, shown right after the token is made:
// it is never shown again.
export function ConnectAgent({ token }) {
  const [how, setHow] = useState('run')
  return (
    <div className="docker-connect">
      <p>
        This token is shown <strong>once</strong>. Run the agent on the Docker host with it; the agent only makes
        outbound HTTPS requests to Watchly, so nothing has to be opened on the host.
      </p>
      <CopyBlock text={token} label="Agent token" />
      <div className="segmented" role="group" aria-label="How to run the agent">
        {[
          ['run', 'docker run'],
          ['compose', 'docker compose'],
        ].map(([key, label]) => (
          <button
            key={key}
            type="button"
            className={how === key ? 'segmented-btn active' : 'segmented-btn'}
            aria-pressed={how === key}
            onClick={() => setHow(key)}
          >
            {label}
          </button>
        ))}
      </div>
      <CopyBlock
        text={how === 'run' ? dockerRunSnippet(token) : composeSnippet(token)}
        label={how === 'run' ? 'docker run command' : 'docker-compose service'}
      />
      <p className="muted small">
        Add <code>watchly.ignore=true</code> as a label to a container to keep it out of Watchly. To check the setup
        first, replace <code>-d</code> with <code>--rm</code> and add <code>check</code> at the end. More, including a
        socket proxy for a stricter setup: <a href={AGENT_REPO}>the agent&apos;s README</a>.
      </p>
    </div>
  )
}

// --- charts --------------------------------------------------------------------

function useWidth() {
  const ref = useRef(null)
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width))
    observer.observe(ref.current)
    return () => observer.disconnect()
  }, [])
  return [ref, width]
}

function niceTop(max) {
  const raw = Math.max(max, 1e-9) / 4
  const power = 10 ** Math.floor(Math.log10(raw))
  const f = raw / power
  const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * power
  return { top: Math.max(Math.ceil(max / step), 1) * step, step }
}

const PAD = { top: 10, right: 12, left: 64, bottom: 24 }
const PLOT = 150
const TIME = { hour: '2-digit', minute: '2-digit' }
const DAY = { month: 'short', day: 'numeric' }
const FULL = { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }

// Up to two lines over time, e.g. average and peak. `series` is
// [{ key, label, className }]; `format` turns a value into text; `max` caps
// the top of the scale (100 for a percentage); `binary` steps the scale in
// KiB/MiB/GiB so byte axes read 8, 16, 24 MiB rather than 9.5, 19.1.
export function MetricChart({ points, series, format, max, label, daily, binary }) {
  const [ref, width] = useWidth()
  const [active, setActive] = useState(null)
  const n = points.length
  const plotWidth = Math.max(width - PAD.left - PAD.right, 0)
  const x = (i) => PAD.left + (n <= 1 ? plotWidth / 2 : (i * plotWidth) / (n - 1))
  const peak = Math.max(0, ...points.flatMap((p) => series.map((s) => p[s.key] ?? 0)))
  const unit = binary ? 1024 ** Math.max(Math.floor(Math.log(Math.max(peak, 1)) / Math.log(1024)), 0) : 1
  const nice = niceTop((max ? Math.min(Math.max(peak * 1.1, max / 10), max) : peak) / unit)
  const top = nice.top * unit
  const step = nice.step * unit
  const y = (v) => PAD.top + PLOT * (1 - v / top)
  const ticks = []
  for (let v = 0; v <= top + step / 2; v += step) ticks.push(v)

  const path = (key) => {
    let d = ''
    let drawing = false
    points.forEach((p, i) => {
      if (p[key] == null) {
        drawing = false
        return
      }
      d += `${drawing ? 'L' : 'M'}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`
      drawing = true
    })
    return d
  }
  const xTicks = []
  const every = Math.max(1, Math.ceil(n / Math.max(Math.floor(plotWidth / 90), 1)))
  for (let i = n - 1; i >= 0; i -= every) xTicks.push(i)
  const tickFormat = daily ? DAY : TIME

  const pick = (event) => {
    if (n === 0) return
    const left = ref.current.getBoundingClientRect().left
    const i = Math.round(((event.clientX - left - PAD.left) / Math.max(plotWidth, 1)) * (n - 1))
    setActive(Math.min(Math.max(i, 0), n - 1))
  }
  const p = active != null ? points[active] : null
  return (
    <div
      ref={ref}
      className="chart"
      tabIndex={0}
      aria-label={label}
      onPointerMove={pick}
      onPointerLeave={() => setActive(null)}
    >
      {width > 0 && (
        <svg width={width} height={PAD.top + PLOT + PAD.bottom} aria-hidden="true">
          {ticks.map((v) => (
            <g key={v}>
              <line className="chart-grid" x1={PAD.left} x2={width - PAD.right} y1={y(v)} y2={y(v)} />
              <text className="chart-tick" x={PAD.left - 8} y={y(v)} textAnchor="end" dominantBaseline="middle">
                {format(v)}
              </text>
            </g>
          ))}
          {series.map((s) => (
            <g key={s.key} className={s.className}>
              <path className="chart-line" d={path(s.key)} />
            </g>
          ))}
          {xTicks.map((i) => (
            <text
              key={i}
              className="chart-tick"
              x={x(i)}
              y={PAD.top + PLOT + 18}
              textAnchor={i === n - 1 && n > 1 ? 'end' : i === 0 ? 'start' : 'middle'}
            >
              {timeFormat(tickFormat).format(new Date(points[i].at))}
            </text>
          ))}
          {p && (
            <g>
              <line className="chart-crosshair" x1={x(active)} x2={x(active)} y1={PAD.top} y2={PAD.top + PLOT} />
              {series.map(
                (s) =>
                  p[s.key] != null && (
                    <circle key={s.key} className={`chart-dot is-active ${s.className}`} cx={x(active)} cy={y(p[s.key])} r={4} />
                  ),
              )}
            </g>
          )}
        </svg>
      )}
      {p && (
        <div
          className="chart-tip"
          style={{ left: x(active), transform: `translateX(${x(active) < width / 2 ? '12px' : 'calc(-100% - 12px)'})` }}
        >
          <div className="muted">{timeFormat(daily ? DAY : FULL).format(new Date(p.at))}</div>
          {series.map((s) => (
            <div key={s.key} className={`chart-tip-row ${s.className}`}>
              <span className="line-key" />
              <strong>{p[s.key] == null ? '—' : format(p[s.key])}</strong>
              <span className="muted">{s.label}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
