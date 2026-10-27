import { useEffect, useId, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { checkType } from './checkTypes.js'
import { environmentLabel } from './environments.js'
import { duration, initials, since, timeAgo } from './format.js'
import { passwordRules } from './password.js'
import { ROLES, roleClass } from './roles.js'
import Select from './Select.jsx'
import SiteIcon from './SiteIcon.jsx'

export function RolePill({ role }) {
  return <span className={`role-pill ${roleClass(role)}`}>{role}</span>
}

// A role picker that looks like RolePill, for the rows the viewer may change.
export function RoleSelect({ value, onChange, disabled, label, roles = ROLES }) {
  return (
    <Select
      className={`role-select ${roleClass(value)}`}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      aria-label={label}
    >
      {roles.map((r) => (
        <option key={r}>{r}</option>
      ))}
    </Select>
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

// How a site is checked: HTTP, Ping, DNS with the record it watches, or DB.
export function CheckTypeBadge({ site }) {
  const type = checkType(site.check_type)
  return (
    <span className={`badge badge-type badge-type-${type.value}`} title={type.description}>
      {type.short}
      {site.dns_record_type && ` ${site.dns_record_type}`}
    </span>
  )
}

// A ring that turns while `busy` is true. When the request ends well it closes
// into a full ring around a tick for a moment, then goes. Renders nothing the
// rest of the time, so it can sit next to a label without moving it. Pass
// `failed` (usually `!!error`) so a rejected request does not look saved.
export function BusyRing({ busy, failed = false }) {
  const [phase, setPhase] = useState(busy ? 'busy' : 'idle')
  const [wasBusy, setWasBusy] = useState(busy)
  if (busy !== wasBusy) {
    setWasBusy(busy)
    setPhase(busy ? 'busy' : failed ? 'idle' : 'done')
  }

  useEffect(() => {
    if (phase !== 'done') return
    const timer = setTimeout(() => setPhase('idle'), 1400)
    return () => clearTimeout(timer)
  }, [phase])

  if (phase === 'idle') return null
  return (
    <svg className={`busy-ring is-${phase}`} viewBox="0 0 24 24" aria-hidden="true">
      <circle className="busy-ring-track" cx="12" cy="12" r="9.5" />
      <circle className="busy-ring-arc" cx="12" cy="12" r="9.5" pathLength="100" />
      {phase === 'done' && <path className="busy-ring-tick" d="M7.5 12.5l3 3 6-6.5" pathLength="100" />}
    </svg>
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

// A modal that guards a destructive action: the confirm button stays disabled
// until the user types the word. `onConfirm` runs the action; the caller closes
// the dialog through `onCancel` or by navigating away. `error` is the page's
// action error: once the user has confirmed, it shows here, since the dialog
// covers the page's own banner.
export function ConfirmDialog({
  title,
  children,
  word = 'confirm',
  confirmLabel = 'Delete',
  busy = false,
  error = null,
  onConfirm,
  onCancel,
}) {
  const [typed, setTyped] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const inputId = useId()
  const ready = typed.trim().toLowerCase() === word

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && !busy && onCancel()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [busy, onCancel])

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && !busy && onCancel()}>
      <form
        className="modal card"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={`${inputId}-title`}
        onSubmit={(e) => {
          e.preventDefault()
          if (ready && !busy) {
            setConfirmed(true)
            onConfirm()
          }
        }}
      >
        <h2 id={`${inputId}-title`}>{title}</h2>
        <div className="modal-body muted small">{children}</div>
        {confirmed && !busy && <ErrorBanner error={error} />}
        <label className="field" htmlFor={inputId}>
          <span>
            Type <strong>{word}</strong> to continue
          </span>
          <input
            id={inputId}
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            autoComplete="off"
            autoFocus
          />
        </label>
        <div className="modal-actions">
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="btn btn-danger-solid" disabled={!ready || busy}>
            {confirmLabel}
          </button>
        </div>
      </form>
    </div>
  )
}

// Clipboard API needs https or localhost; on plain http fall back to a
// throwaway textarea so the button still works.
async function writeClipboard(text) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Denied: try the fallback below.
    }
  }
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.opacity = '0'
  document.body.appendChild(area)
  area.select()
  try {
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    area.remove()
  }
}

// `copy(text)` puts text on the clipboard; `copied` stays true for a moment after.
export function useCopy() {
  const [copied, setCopied] = useState(false)
  const timer = useRef(null)
  useEffect(() => () => clearTimeout(timer.current), [])
  const copy = async (text) => {
    if (!(await writeClipboard(text))) return
    setCopied(true)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setCopied(false), 1800)
  }
  return [copied, copy]
}

// A small button that copies `text` and says "Copied" for a moment.
export function CopyButton({ text, label = 'value' }) {
  const [copied, copy] = useCopy()
  return (
    <>
      <button
        type="button"
        className={copied ? 'copy-btn copied' : 'copy-btn'}
        onClick={() => copy(text)}
        aria-label={`Copy ${label}`}
        title={`Copy ${label}`}
      >
        {copied ? (
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="m5 12 5 5 9-10" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <rect x="9" y="9" width="11" height="11" rx="2" />
            <path d="M5 15V6a2 2 0 0 1 2-2h9" />
          </svg>
        )}
        {copied && <span className="copy-done">Copied</span>}
      </button>
      <span className="visually-hidden" role="status">
        {copied ? `Copied ${label}` : ''}
      </span>
    </>
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

// Where this page sits: `items` run from the top down, each `{ label, to }`.
// The last one is the page itself and has no `to`.
export function Breadcrumbs({ items }) {
  return (
    <nav className="crumbs" aria-label="Breadcrumb">
      <ol>
        {items.map((item) => (
          <li key={item.to ?? item.label}>
            {item.to ? (
              <Link to={item.to} title={item.label}>
                {item.label}
              </Link>
            ) : (
              <span aria-current="page" title={item.label}>
                {item.label}
              </span>
            )}
          </li>
        ))}
      </ol>
    </nav>
  )
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>
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
                <div className="site-name">
                  <SiteIcon site={site} />
                  <div className="site-name-text">
                    <Link to={`/websites/${site.id}`} className="strong-link">
                      {site.name}
                    </Link>
                    <div className="muted small truncate">{site.url}</div>
                  </div>
                </div>
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

// "1–50 of 106" with Previous and Next, for a list the API serves a page at a
// time. Draws nothing while everything fits on one page.
export function Pager({ page, pageSize, total, onPage }) {
  if (total <= pageSize) return null
  const lastPage = Math.max(Math.ceil(total / pageSize) - 1, 0)
  return (
    <div className="pager">
      <span className="muted small">
        {page * pageSize + 1}–{Math.min((page + 1) * pageSize, total)} of {total}
      </span>
      <button type="button" className="btn btn-sm" onClick={() => onPage(page - 1)} disabled={page === 0}>
        Previous
      </button>
      <button type="button" className="btn btn-sm" onClick={() => onPage(page + 1)} disabled={page >= lastPage}>
        Next
      </button>
    </div>
  )
}

// Under a new password: each rule, ticked as soon as the password meets it.
// `identity` is what is known of the account: { username, email, fullName }.
export function PasswordChecklist({ password, identity, id }) {
  const rules = passwordRules(password, identity)
  return (
    <ul className="password-rules" id={id} aria-label="Password requirements">
      {rules.map((rule) => (
        <li key={rule.key} className={rule.met ? 'is-met' : undefined}>
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <circle cx="8" cy="8" r="7" />
            <path d="m5 8.2 2 2 4-4.4" />
          </svg>
          {rule.label}
          <span className="visually-hidden">{rule.met ? ' (done)' : ' (not yet)'}</span>
        </li>
      ))}
    </ul>
  )
}
