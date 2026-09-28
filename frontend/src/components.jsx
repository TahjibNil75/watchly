import { Link } from 'react-router-dom'
import { checkType } from './checkTypes.js'
import { environmentLabel } from './environments.js'
import { duration, initials, since, timeAgo } from './format.js'
import { ROLES, roleClass } from './roles.js'

export function RolePill({ role }) {
  return <span className={`role-pill ${roleClass(role)}`}>{role}</span>
}

// A role picker that looks like RolePill, for the rows the viewer may change.
export function RoleSelect({ value, onChange, disabled, label }) {
  return (
    <span className={`role-select ${roleClass(value)}`}>
      <select
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        aria-label={label}
      >
        {ROLES.map((r) => (
          <option key={r}>{r}</option>
        ))}
      </select>
    </span>
  )
}

// A person's initials in their role's colour. `className` adds a size, e.g. avatar-lg.
export function Avatar({ user, className = '' }) {
  return (
    <span className={`avatar ${roleClass(user.role)} ${className}`.trim()} aria-hidden="true">
      {initials(user.full_name || user.username)}
    </span>
  )
}

// `maintenance` is the site's window in effect, if any: it outranks the
// status, which is not being checked meanwhile.
export function StatusBadge({ status, enabled = true, maintenance = null }) {
  if (!enabled) return <span className="badge badge-paused">paused</span>
  if (maintenance) return <span className="badge badge-maintenance">maintenance</span>
  const label = status === 'unknown' ? 'pending' : status
  return <span className={`badge badge-${status}`}>{label}</span>
}

// Which deployment a site is. Renders nothing for sites that predate the field.
export function EnvironmentBadge({ environment }) {
  const label = environmentLabel(environment)
  if (!label) return null
  return <span className={`badge badge-env badge-env-${environment}`}>{label}</span>
}

// How a site is checked: HTTP, Ping, or DNS with the record it watches.
export function CheckTypeBadge({ site }) {
  const type = checkType(site.check_type)
  return (
    <span className={`badge badge-type badge-type-${type.value}`} title={type.description}>
      {type.short}
      {site.dns_record_type && ` ${site.dns_record_type}`}
    </span>
  )
}

export function ErrorBanner({ error }) {
  if (!error) return null
  return (
    <div className="banner banner-error" role="alert">
      {error.message ?? String(error)}
    </div>
  )
}

export function Loading({ label = 'Loading…' }) {
  return <p className="muted loading">{label}</p>
}

export function PageHeader({ title, subtitle, children }) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle && <p className="muted">{subtitle}</p>}
      </div>
      {children && <div className="actions">{children}</div>}
    </header>
  )
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>
}

// Multi-select over users as a scrollable checkbox list.
export function UserChecklist({ users, selected, onChange, emptyLabel = 'No users to add.' }) {
  if (!users.length) return <p className="muted small">{emptyLabel}</p>
  const toggle = (id) =>
    onChange(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id])
  return (
    <div className="checklist">
      {users.map((u) => (
        <label key={u.id} className="checklist-item">
          <input type="checkbox" checked={selected.includes(u.id)} onChange={() => toggle(u.id)} />
          <span>
            {u.full_name || u.username}
            <span className="muted small"> · {u.email}</span>
          </span>
        </label>
      ))}
    </div>
  )
}

export function PersonList({ people, onRemove, emptyLabel }) {
  if (!people.length) return <p className="muted small">{emptyLabel}</p>
  return (
    <ul className="people">
      {people.map((p) => (
        <li key={p.id}>
          <div>
            <strong>{p.full_name || p.username}</strong>
            {!p.is_active && <span className="badge badge-down">suspended</span>}
            <div className="muted small">{p.email}</div>
          </div>
          {onRemove && (
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => onRemove(p)}>
              Remove
            </button>
          )}
        </li>
      ))}
    </ul>
  )
}

export function WebsiteTable({ sites, projectNames, emptyLabel }) {
  if (!sites.length) return <Empty>{emptyLabel}</Empty>
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Status</th>
            <th>Website</th>
            <th>Type</th>
            <th>Environment</th>
            {projectNames && <th>Project</th>}
            <th>Last check</th>
            <th>Interval</th>
            <th>Outage</th>
          </tr>
        </thead>
        <tbody>
          {sites.map((site) => (
            <tr key={site.id}>
              <td>
                <StatusBadge
                  status={site.status}
                  enabled={site.is_enabled}
                  maintenance={site.maintenance}
                />
              </td>
              <td>
                <Link to={`/websites/${site.id}`} className="strong-link">
                  {site.name}
                </Link>
                <div className="muted small truncate">{site.url}</div>
              </td>
              <td>
                <CheckTypeBadge site={site} />
              </td>
              <td>
                <EnvironmentBadge environment={site.environment} />
                {!site.environment && <span className="muted">—</span>}
              </td>
              {projectNames && (
                <td>
                  <Link to={`/projects/${site.project_id}`}>
                    {projectNames[site.project_id] ?? `#${site.project_id}`}
                  </Link>
                </td>
              )}
              <td className="nowrap">{timeAgo(site.last_checked_at)}</td>
              <td className="nowrap">every {duration(site.check_interval_seconds)}</td>
              <td className="nowrap">
                {site.down_since ? (
                  <span className="text-down">down {since(site.down_since)}</span>
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
