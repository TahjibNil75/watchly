import { useLayoutEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { Empty, ErrorBanner, Loading } from './components.jsx'
import { saveFile } from './download.js'
import { useApi } from './useApi.js'

const RANGES = [
  { key: '24h', label: '24 hours' },
  { key: '7d', label: '7 days' },
  { key: '30d', label: '30 days' },
  { key: '90d', label: '90 days' },
]

const SERIES = [
  { key: 'avg_response_ms', label: 'Average', className: 'series-1' },
  { key: 'p95_response_ms', label: '95th percentile', className: 'series-2' },
]

const UPTIME_TIERS = [
  { className: 'is-up', label: '100%' },
  { className: 'is-degraded', label: '99–99.99%' },
  { className: 'is-down', label: 'under 99%' },
  { className: 'is-empty', label: 'no checks' },
]

const uptimeTier = (b) =>
  b.checks === 0
    ? 'is-empty'
    : b.uptime_percent === 100
      ? 'is-up'
      : b.uptime_percent >= 99
        ? 'is-degraded'
        : 'is-down'

// Floored, so 99.996% never reads as a perfect 100%.
const percent = (value) =>
  value == null ? '—' : value === 100 ? '100%' : `${(Math.floor(value * 100) / 100).toFixed(2)}%`
const ms = (value) => (value == null ? '—' : `${value.toLocaleString()} ms`)
const tickMs = (value) => (value >= 1000 ? `${value / 1000} s` : `${value} ms`)
const loss = (value) => (value == null ? '—' : `${Math.round(value * 100) / 100}%`)

// Buckets are UTC hours or days, so they are labelled in UTC too.
const utc = (options) => new Intl.DateTimeFormat(undefined, { timeZone: 'UTC', ...options })
const HOUR_TICK = utc({ hour: '2-digit', minute: '2-digit' })
const DAY_TICK = utc({ month: 'short', day: 'numeric' })
const HOUR_FULL = utc({ weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
const DAY_FULL = utc({ weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' })

const bucketLabel = (stats, iso) =>
  stats.bucket_seconds === 3600
    ? `${HOUR_FULL.format(new Date(iso))} UTC`
    : DAY_FULL.format(new Date(iso))

// Which buckets get an x-axis label, before thinning to fit the width.
function isTick(stats, bucket, index) {
  const fromEnd = stats.series.length - 1 - index
  const hour = new Date(bucket.start).getUTCHours()
  if (stats.range === '24h') return hour % 6 === 0
  if (stats.range === '7d') return hour === 0
  return fromEnd % (stats.range === '30d' ? 7 : 15) === 0
}

// 0 and clean steps (1, 2 or 5 × 10ⁿ) up to just past `max`.
function yScale(max) {
  const raw = Math.max(max, 1) / 4
  const power = 10 ** Math.floor(Math.log10(raw))
  const f = raw / power
  const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * power
  const top = Math.max(Math.ceil(max / step), 1) * step
  const ticks = []
  for (let v = 0; v <= top; v += step) ticks.push(v)
  return { top, ticks }
}

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

const PAD = { top: 10, right: 12, left: 56 }
const PLOT_HEIGHT = 170
const STRIP_TOP = PAD.top + PLOT_HEIGHT + 10
const STRIP_HEIGHT = 14
const AXIS_Y = STRIP_TOP + STRIP_HEIGHT + 16
const HEIGHT = AXIS_Y + 6
const MIN_TICK_GAP = 64

// Response time as lines, with each bucket's uptime as a strip beneath them
// on the same x positions; one crosshair and tooltip read both.
function HistoryChart({ stats, ping }) {
  const [ref, width] = useWidth()
  const [active, setActive] = useState(null)
  const series = stats.series
  const n = series.length
  const plotWidth = Math.max(width - PAD.left - PAD.right, 0)
  const step = plotWidth / n
  const x = (i) => PAD.left + (i + 0.5) * step
  const { top, ticks } = yScale(
    Math.max(0, ...series.flatMap((b) => SERIES.map((s) => b[s.key] ?? 0))),
  )
  const y = (v) => PAD.top + PLOT_HEIGHT * (1 - v / top)

  // Empty buckets break the line instead of bridging the gap.
  const path = (key) => {
    let d = ''
    let drawing = false
    series.forEach((b, i) => {
      if (b[key] == null) {
        drawing = false
        return
      }
      d += `${drawing ? 'L' : 'M'}${x(i).toFixed(1)},${y(b[key]).toFixed(1)}`
      drawing = true
    })
    return d
  }
  // A point with no neighbour draws no segment, so it gets a dot.
  const lonePoints = (key) =>
    series
      .map((b, i) => i)
      .filter(
        (i) => series[i][key] != null && series[i - 1]?.[key] == null && series[i + 1]?.[key] == null,
      )

  // Newest first, so the latest label is kept when thinning.
  const xTicks = []
  for (let i = n - 1; i >= 0; i--) {
    if (!isTick(stats, series[i], i)) continue
    if (xTicks.length && x(xTicks.at(-1)) - x(i) < MIN_TICK_GAP) continue
    if (x(i) < PAD.left + 20) continue
    xTicks.push(i)
  }
  const tickFormat = stats.range === '24h' ? HOUR_TICK : DAY_TICK
  const barGap = step >= 6 ? 2 : step >= 3 ? 1 : 0

  const pick = (event) => {
    const left = ref.current.getBoundingClientRect().left
    const i = Math.floor((event.clientX - left - PAD.left) / step)
    setActive(i < 0 || i >= n ? null : i)
  }
  const onKeyDown = (event) => {
    const moves = {
      ArrowLeft: (i) => Math.max((i ?? n) - 1, 0),
      ArrowRight: (i) => Math.min((i ?? -1) + 1, n - 1),
      Home: () => 0,
      End: () => n - 1,
      Escape: () => null,
    }
    if (!moves[event.key]) return
    event.preventDefault()
    setActive(moves[event.key])
  }

  const b = active != null ? series[active] : null
  return (
    <div
      ref={ref}
      className="chart"
      tabIndex={0}
      aria-label="Response time and uptime chart. Arrow keys step through the buckets."
      onPointerMove={pick}
      onPointerLeave={() => setActive(null)}
      onFocus={() => setActive((i) => i ?? n - 1)}
      onBlur={() => setActive(null)}
      onKeyDown={onKeyDown}
    >
      {width > 0 && (
        <svg width={width} height={HEIGHT} aria-hidden="true">
          {ticks.map((v) => (
            <g key={v}>
              <line className="chart-grid" x1={PAD.left} x2={width - PAD.right} y1={y(v)} y2={y(v)} />
              <text className="chart-tick" x={PAD.left - 8} y={y(v)} textAnchor="end" dominantBaseline="middle">
                {tickMs(v)}
              </text>
            </g>
          ))}
          {SERIES.map((s) => (
            <g key={s.key} className={s.className}>
              <path className="chart-line" d={path(s.key)} />
              {lonePoints(s.key).map((i) => (
                <circle key={i} className="chart-dot" cx={x(i)} cy={y(series[i][s.key])} r={3} />
              ))}
            </g>
          ))}

          <text className="chart-tick" x={PAD.left - 8} y={STRIP_TOP + STRIP_HEIGHT / 2} textAnchor="end" dominantBaseline="middle">
            Uptime
          </text>
          {series.map((bucket, i) => (
            <rect
              key={bucket.start}
              className={`uptime-bar ${uptimeTier(bucket)}`}
              x={PAD.left + i * step + barGap / 2}
              y={STRIP_TOP}
              width={Math.max(step - barGap, 0.5)}
              height={STRIP_HEIGHT}
              rx={Math.min(2, step / 4)}
            />
          ))}

          {xTicks.map((i) => (
            <text key={i} className="chart-tick" x={x(i)} y={AXIS_Y} textAnchor="middle">
              {tickFormat.format(new Date(series[i].start))}
            </text>
          ))}

          {b && (
            <g>
              <line className="chart-crosshair" x1={x(active)} x2={x(active)} y1={PAD.top} y2={STRIP_TOP + STRIP_HEIGHT} />
              <rect
                className="uptime-bar-ring"
                x={PAD.left + active * step}
                y={STRIP_TOP - 2}
                width={Math.max(step, 2)}
                height={STRIP_HEIGHT + 4}
                rx={3}
              />
              {SERIES.map(
                (s) =>
                  b[s.key] != null && (
                    <circle key={s.key} className={`chart-dot is-active ${s.className}`} cx={x(active)} cy={y(b[s.key])} r={4} />
                  ),
              )}
            </g>
          )}
        </svg>
      )}
      {b && (
        <div
          className="chart-tip"
          style={{
            left: x(active),
            transform: `translateX(${x(active) < width / 2 ? '12px' : 'calc(-100% - 12px)'})`,
          }}
        >
          <div className="muted">{bucketLabel(stats, b.start)}</div>
          {SERIES.map((s) => (
            <div key={s.key} className={`chart-tip-row ${s.className}`}>
              <span className="line-key" />
              <strong>{ms(b[s.key])}</strong>
              <span className="muted">{s.label}</span>
            </div>
          ))}
          <div className="chart-tip-row">
            <span className={`swatch ${uptimeTier(b)}`} />
            <strong>{percent(b.uptime_percent)}</strong>
            <span className="muted">
              uptime · {b.checks.toLocaleString()} {b.checks === 1 ? 'check' : 'checks'}
            </span>
          </div>
          {ping && b.packet_loss_percent != null && (
            <div className="chart-tip-row">
              {/* Not drawn in the chart, so a plain key. */}
              <span className="swatch" />
              <strong>{loss(b.packet_loss_percent)}</strong>
              <span className="muted">packet loss</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function HistoryTable({ stats, ping }) {
  // Newest first, like the checks table.
  const rows = [...stats.series].reverse().filter((b) => b.checks > 0)
  return (
    <details className="advanced">
      <summary>Show as table</summary>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>{stats.bucket_seconds === 3600 ? 'Hour' : 'Day'}</th>
              <th>Checks</th>
              <th>Uptime</th>
              <th>Average</th>
              <th>95th percentile</th>
              {ping && <th>Packet loss</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((b) => (
              <tr key={b.start}>
                <td className="nowrap">{bucketLabel(stats, b.start)}</td>
                <td className="num">{b.checks.toLocaleString()}</td>
                <td className="num">{percent(b.uptime_percent)}</td>
                <td className="num">{ms(b.avg_response_ms)}</td>
                <td className="num">{ms(b.p95_response_ms)}</td>
                {ping && <td className="num">{loss(b.packet_loss_percent)}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  )
}

// `ping`: the site is a pinged host, so its times are round trips and it has
// packet loss to show. `dns`: a DNS check, whose times are the resolvers'
// average answer time.
export default function SiteHistory({ websiteId, ping = false, dns = false }) {
  const [range, setRange] = useState('24h')
  const [downloadError, setDownloadError] = useState(null)
  // The rollup behind these moves once a minute, with the scheduler.
  const stats = useApi(() => api.getWebsiteStats(websiteId, range), [websiteId, range], {
    pollMs: 60000,
  })
  const s = stats.data
  const label = RANGES.find((r) => r.key === range).label

  async function download() {
    setDownloadError(null)
    try {
      saveFile(`watchly-site-${websiteId}-${range}.csv`, await api.websiteStatsCsv(websiteId, range))
    } catch (error) {
      setDownloadError(error)
    }
  }

  return (
    <section className="card">
      <h2>History</h2>
      <div className="tabs" role="tablist" aria-label="Range">
        {RANGES.map((r) => (
          <button
            key={r.key}
            type="button"
            role="tab"
            aria-selected={range === r.key}
            className={range === r.key ? 'tab active' : 'tab'}
            onClick={() => setRange(r.key)}
          >
            {r.label}
          </button>
        ))}
      </div>
      <ErrorBanner error={stats.error ?? downloadError} />
      {!s ? (
        stats.loading && <Loading />
      ) : (
        // While another range loads, the last one stays up, dimmed.
        <div className={s.range === range ? undefined : 'is-stale'}>
          <div className="kv kv-inline">
            <div>
              <span>Uptime</span>
              <strong>{percent(s.uptime_percent)}</strong>
            </div>
            <div>
              <span>{ping ? 'Avg round trip' : dns ? 'Avg answer time' : 'Avg response'}</span>
              <strong>{ms(s.avg_response_ms)}</strong>
            </div>
            <div>
              <span>95th percentile</span>
              <strong>{ms(s.p95_response_ms)}</strong>
            </div>
            {ping && (
              <div>
                <span>Packet loss</span>
                <strong>{loss(s.packet_loss_percent)}</strong>
              </div>
            )}
            <div>
              <span>Checks</span>
              <strong>{s.checks.toLocaleString()}</strong>
            </div>
            <button type="button" className="btn btn-sm kv-action" onClick={download}>
              Download CSV
            </button>
          </div>
          {s.checks === 0 ? (
            <Empty>No checks in the last {label}.</Empty>
          ) : (
            <>
              <div className="chart-legend">
                {SERIES.map((series) => (
                  <span key={series.key} className={series.className}>
                    <span className="line-key" /> {series.label}
                  </span>
                ))}
                <span className="chart-legend-sep" aria-hidden="true" />
                {UPTIME_TIERS.map((t) => (
                  <span key={t.className}>
                    <span className={`swatch ${t.className}`} /> {t.label}
                  </span>
                ))}
              </div>
              <HistoryChart stats={s} ping={ping} />
              <p className="muted small">
                {ping
                  ? 'Times are UTC. Round trips are each check’s average, over checks that got a reply, in whole milliseconds; '
                  : dns
                    ? 'Times are UTC. Answer times are each check’s average over its resolvers, for successful checks only; '
                    : 'Times are UTC. Response times count successful checks only; '}
                the 95th percentile is read from response-time buckets, so it is accurate to within
                about 25%.
              </p>
              <HistoryTable stats={s} ping={ping} />
            </>
          )}
        </div>
      )}
    </section>
  )
}
