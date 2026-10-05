// The infrastructure pages' shared components: badges, the resource table and
// the step-by-step diagnose result. Their labels live in infra.js.
import { Link } from 'react-router-dom'
import { EnvironmentBadge, Empty } from './components.jsx'
import { since, timeAgo } from './format.js'
import { AWS_REGIONS, CHECK_TYPES, PROVIDERS, exposureLine, kindInfo, resourceTrouble, stateLabel } from './infra.js'

export function StateBadge({ state }) {
  return <span className={`badge badge-${state}`}>{stateLabel(state)}</span>
}

export function HealthBadge({ health }) {
  const label = { healthy: 'Healthy', degraded: 'Degraded', down: 'Down', unknown: 'Pending' }[health] ?? health
  return <span className={`badge badge-${health}`}>{label}</span>
}

export function KindBadge({ kind }) {
  return <span className={`badge badge-type badge-kind-${kind}`}>{kindInfo(kind).label}</span>
}

export function CheckTypeBadge({ type }) {
  return (
    <span className={`badge badge-type badge-check-${type}`} title={CHECK_TYPES[type]?.note}>
      {CHECK_TYPES[type]?.label ?? type}
    </span>
  )
}

// Which cloud to read from. The providers not built yet are shown, disabled.
export function ProviderPicker({ value, onChange }) {
  return (
    <fieldset className="fieldset">
      <legend>Cloud provider</legend>
      <div className="choices provider-choices" role="radiogroup" aria-label="Cloud provider">
        {PROVIDERS.map((p) => (
          <label key={p.value} className={`choice provider-choice${p.available ? '' : ' is-disabled'}`}>
            <input
              type="radio"
              name="provider"
              value={p.value}
              checked={value === p.value}
              disabled={!p.available}
              onChange={() => onChange(p.value)}
            />
            <span className="provider-mark" aria-hidden="true">
              {p.mark}
            </span>
            <span>
              <strong>
                {p.label}
                {!p.available && <span className="badge badge-unknown provider-soon">Coming soon</span>}
              </strong>
              <span className="muted small">{p.note}</span>
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  )
}

// A drop-down of AWS regions. The empty value means "use the default", named
// by `emptyLabel`.
export function RegionSelect({ value, onChange, emptyLabel, ...rest }) {
  const known = AWS_REGIONS.some((g) => g.regions.some(([code]) => code === value))
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} {...rest}>
      <option value="">{emptyLabel}</option>
      {value && !known && <option value={value}>{value}</option>}
      {AWS_REGIONS.map((g) => (
        <optgroup key={g.group} label={g.group}>
          {g.regions.map(([code, name]) => (
            <option key={code} value={code}>
              {code} · {name}
            </option>
          ))}
        </optgroup>
      ))}
    </select>
  )
}

export function InfraIcon({ className, children }) {
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

const KIND_ICONS = {
  server: (
    <>
      <rect x="3.5" y="4" width="17" height="6.5" rx="1.5" />
      <rect x="3.5" y="13.5" width="17" height="6.5" rx="1.5" />
      <path d="M7.5 7.25h.01M7.5 16.75h.01" />
    </>
  ),
  auto_scaling_group: (
    <>
      <rect x="2.5" y="8" width="11" height="5" rx="1.2" />
      <rect x="2.5" y="15" width="11" height="5" rx="1.2" />
      <path d="M2.5 5.5V5a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v.5M18.5 9v10M15.5 12l3-3 3 3M15.5 16l3 3 3-3" />
    </>
  ),
  load_balancer: (
    <>
      <circle cx="12" cy="5" r="2.5" />
      <circle cx="5" cy="19" r="2.5" />
      <circle cx="12" cy="19" r="2.5" />
      <circle cx="19" cy="19" r="2.5" />
      <path d="M12 7.5v9M12 10l-7 6.5M12 10l7 6.5" />
    </>
  ),
  database: (
    <>
      <ellipse cx="12" cy="5.5" rx="7.5" ry="2.75" />
      <path d="M4.5 5.5v13c0 1.5 3.4 2.75 7.5 2.75s7.5-1.25 7.5-2.75v-13M4.5 12c0 1.5 3.4 2.75 7.5 2.75s7.5-1.25 7.5-2.75" />
    </>
  ),
}

export function KindIcon({ kind, className }) {
  return <InfraIcon className={className}>{KIND_ICONS[kind]}</InfraIcon>
}

export function ResourceTable({ resources, emptyLabel, showVpc = true }) {
  if (!resources.length) return <Empty>{emptyLabel}</Empty>
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>State</th>
            <th>Resource</th>
            <th>Kind</th>
            <th>Environment</th>
            {showVpc && <th>VPC</th>}
            <th>Project</th>
            <th>Checks</th>
            <th>Last check</th>
          </tr>
        </thead>
        <tbody>
          {resources.map((r) => {
            const trouble = resourceTrouble(r)
            return (
              <tr key={r.id}>
                <td>
                  <StateBadge state={r.state} />
                </td>
                <td>
                  <Link to={`/infra/resources/${r.id}`} className="strong-link">
                    {r.name}
                  </Link>
                  <div className="muted small truncate">{r.address ?? r.aws_id}</div>
                  {trouble && r.state !== 'healthy' && (
                    <div className={`small truncate ${r.state === 'down' ? 'text-down' : 'text-pending'}`}>
                      {trouble}
                    </div>
                  )}
                </td>
                <td>
                  <KindBadge kind={r.kind} />
                  <div className="muted small nowrap">{exposureLine(r)}</div>
                </td>
                <td>
                  <EnvironmentBadge environment={r.environment} />
                  {!r.environment && <span className="muted">—</span>}
                </td>
                {showVpc && (
                  <td className="nowrap">
                    <Link to={`/infra/vpcs/${r.vpc.id}`}>{r.vpc.name}</Link>
                  </td>
                )}
                <td>
                  <Link to={`/projects/${r.project.id}`}>{r.project.name}</Link>
                </td>
                <td className="nowrap">
                  {r.checks.map((c) => (
                    <span key={c.id} className={`check-dot health-${c.is_enabled ? c.health : 'paused'}`} title={`${c.name}: ${c.last_result?.summary ?? 'not checked yet'}`} />
                  ))}
                </td>
                <td className="nowrap">
                  {r.down_since ? (
                    <span className="text-down">down {since(r.down_since)}</span>
                  ) : (
                    timeAgo(r.last_checked_at)
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// Where a diagnose hint says to look, by the hint's `kind`.
const HINT_LABELS = {
  security_group: 'Security group',
  network_acl: 'Network ACL / firewall',
  route: 'Routes',
  port: 'Port',
}

// A diagnose run, one line per step: what passed, what failed and why.
export function DiagnoseSteps({ result }) {
  if (!result) return null
  return (
    <div className={`diagnose ${result.ok ? 'is-ok' : 'is-failed'}`}>
      <p className="diagnose-summary">
        <strong>{result.ok ? 'Everything answered.' : `Failed at ${result.failed_step}.`}</strong>{' '}
        <span className="muted">{result.summary}</span>
      </p>
      <ol className="steps">
        {result.steps.map((s) => (
          <li key={s.step} className={s.skipped ? 'is-skipped' : s.ok ? 'is-ok' : 'is-failed'}>
            <span className="step-mark" aria-hidden="true">
              {s.skipped ? '·' : s.ok ? '✓' : '✗'}
            </span>
            <span className="step-name">{s.step}</span>
            <span className="step-time muted">{s.time_ms != null ? `${s.time_ms} ms` : ''}</span>
            <span className="step-detail">{s.skipped ? <span className="muted">skipped</span> : s.detail}</span>
          </li>
        ))}
      </ol>
      {result.hints?.map((h) => (
        <p key={`${h.kind}-${h.resource}`} className="banner banner-info small">
          <strong>{HINT_LABELS[h.kind] || 'Look at'}:</strong> {h.detail} <strong>{h.fix}</strong>
        </p>
      ))}
    </div>
  )
}

