import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { api } from './api.js'
import { Avatar } from './components.jsx'
import { roleClass } from './roles.js'

// Choose several people: wherever someone is added as a member, a recipient or
// an assignee. It opens inline like every other drop-down (see Select.jsx),
// with a search on top and, per person, their avatar, name, email and role
// badge in the role's colour.
//
// `users` is the directory already loaded (the API's first page). When that
// page is full there may be more people than it holds, so a search also asks
// the API, and everyone seen stays selectable. `exclude` leaves out people who
// are already added.

// The page size api.listUsers asks for. A full page may be only part of them.
const DIRECTORY_PAGE = 100
const MAX_ROWS = 100
const SEARCH_DELAY_MS = 250
const NONE = []

const nameOf = (u) => u.full_name || u.username

const matches = (u, term) =>
  [u.full_name, u.username, u.email, u.role].some((v) => v && v.toLowerCase().includes(term))

export default function PeoplePicker({
  users,
  selected,
  onChange,
  exclude = [],
  emptyLabel = 'No users to add.',
  placeholder = 'Select people…',
  ...rest
}) {
  const uid = useId()
  const root = useRef(null)
  const trigger = useRef(null)
  const search = useRef(null)
  const list = useRef(null)
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  // What the API returned for one search term, beyond the loaded page.
  const [answer, setAnswer] = useState({ term: '', items: [] })
  // People picked from a search, so they keep a face once it is cleared.
  const [picked, setPicked] = useState({})

  const maybeMore = users.length >= DIRECTORY_PAGE
  const excluded = useMemo(() => new Set(exclude), [exclude])
  const query = q.trim()
  const term = query.toLowerCase()
  const asking = maybeMore && Boolean(term)
  const found = asking && answer.term === query ? answer.items : NONE
  const searching = asking && answer.term !== query

  useEffect(() => {
    if (!asking) return
    let stale = false
    const timer = setTimeout(() => {
      api
        .listUsers({ is_active: true, q: query })
        .then((res) => !stale && setAnswer({ term: query, items: res.items }))
        // The loaded page still answers; a failed search only finds less.
        .catch(() => !stale && setAnswer({ term: query, items: [] }))
    }, SEARCH_DELAY_MS)
    return () => {
      stale = true
      clearTimeout(timer)
    }
  }, [asking, query])

  const visible = useMemo(() => {
    const local = term ? users.filter((u) => matches(u, term)) : users
    const have = new Set(local.map((u) => u.id))
    return [...local, ...found.filter((u) => !have.has(u.id))].filter((u) => !excluded.has(u.id))
  }, [users, found, term, excluded])

  const known = (id) => users.find((u) => u.id === id) ?? picked[id]
  const chosen = selected.map(known).filter(Boolean)
  const shown = visible.slice(0, MAX_ROWS)

  const toggle = (u) => {
    setPicked((p) => ({ ...p, [u.id]: u }))
    onChange(selected.includes(u.id) ? selected.filter((x) => x !== u.id) : [...selected, u.id])
  }

  const rows = () => [...list.current.querySelectorAll('[role="option"]')]

  const close = (refocus) => {
    setOpen(false)
    setQ('')
    if (refocus) trigger.current?.focus()
  }

  useEffect(() => {
    if (open) search.current?.focus({ preventScroll: true })
  }, [open])

  useEffect(() => {
    if (!open) return
    const away = (e) => {
      if (!root.current?.contains(e.target)) close(false)
    }
    document.addEventListener('mousedown', away)
    return () => document.removeEventListener('mousedown', away)
  }, [open])

  const onSearchKey = (e) => {
    if (e.key === 'ArrowDown' || e.key === 'Enter') {
      // Enter must not submit a form this picker sits in.
      e.preventDefault()
      rows()[0]?.focus()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      if (q) setQ('')
      else close(true)
    }
  }

  const onRowKey = (e) => {
    const items = rows()
    const at = items.indexOf(document.activeElement)
    if (e.key === 'Tab') return close(false)
    // Any plain character goes to the search, which keeps it.
    if (e.key.length === 1 && e.key !== ' ' && !e.ctrlKey && !e.metaKey && !e.altKey) {
      search.current?.focus()
      return
    }
    if (e.key === 'ArrowDown') items[Math.min(at + 1, items.length - 1)]?.focus()
    else if (e.key === 'ArrowUp') (at <= 0 ? search.current : items[at - 1])?.focus()
    else if (e.key === 'Home') items[0]?.focus()
    else if (e.key === 'End') items[items.length - 1]?.focus()
    else if (e.key === 'Escape') close(true)
    else return
    e.preventDefault()
  }

  if (!users.length && !maybeMore) return <p className="muted small">{emptyLabel}</p>

  const face = chosen.length
    ? chosen.slice(0, 2).map(nameOf).join(', ') + (chosen.length > 2 ? ` +${chosen.length - 2}` : '')
    : placeholder
  const allShownChosen = shown.length > 0 && shown.every((u) => selected.includes(u.id))

  return (
    <div
      ref={root}
      className={`acc-select people-picker${open ? ' is-open' : ''}`}
      onBlur={(e) => {
        if (!root.current.contains(e.relatedTarget)) close(false)
      }}
    >
      <button
        ref={trigger}
        type="button"
        className="acc-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={`${uid}-list`}
        onClick={() => (open ? close(false) : setOpen(true))}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown') {
            e.preventDefault()
            setOpen(true)
          } else if (e.key === 'Escape' && open) {
            e.preventDefault()
            close(false)
          }
        }}
        {...rest}
      >
        {chosen.length > 0 && (
          <span className="people-stack" aria-hidden="true">
            {chosen.slice(0, 3).map((u) => (
              <Avatar key={u.id} user={u} className="avatar-sm" />
            ))}
          </span>
        )}
        <span className={`acc-value${chosen.length ? '' : ' is-empty'}`}>{face}</span>
        {chosen.length > 0 && <span className="people-count">{chosen.length}</span>}
        <svg className="acc-chev" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="m5 8 5 5 5-5" />
        </svg>
      </button>
      <div className="acc-body" inert={!open}>
        <div>
          <div className="people-search">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden="true">
              <circle cx="9" cy="9" r="5.5" />
              <path d="m13.5 13.5 4 4" />
            </svg>
            <input
              ref={search}
              type="text"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={onSearchKey}
              placeholder="Search by name, email or role"
              aria-label="Search people"
              autoComplete="off"
              maxLength={200}
            />
            {q && (
              <button type="button" className="people-clear" aria-label="Clear search" onClick={() => { setQ(''); search.current?.focus() }}>
                ×
              </button>
            )}
          </div>
          <div
            ref={list}
            id={`${uid}-list`}
            className="acc-list"
            role="listbox"
            aria-multiselectable="true"
            aria-label="People"
            onKeyDown={onRowKey}
            // Keeps focus where it is, so a click on a row is not a blur first.
            onMouseDown={(e) => e.preventDefault()}
          >
            {shown.map((u) => {
              const on = selected.includes(u.id)
              return (
                <button
                  key={u.id}
                  type="button"
                  role="option"
                  tabIndex={-1}
                  aria-selected={on}
                  className="acc-opt person-opt"
                  onClick={() => toggle(u)}
                >
                  <span className="box" aria-hidden="true">
                    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                      <path d="m4 10.5 4 4 8-9" />
                    </svg>
                  </span>
                  <Avatar user={u} className="avatar-sm" />
                  <span className="person-text">
                    <span className="person-name">{nameOf(u)}</span>
                    <span className="person-email">{u.email}</span>
                  </span>
                  <span className={`person-role ${roleClass(u.role)}`}>{u.role}</span>
                </button>
              )
            })}
            {!shown.length && (
              <p className="people-empty">
                {searching ? 'Searching…' : term ? `No one matches “${q.trim()}”.` : emptyLabel}
              </p>
            )}
          </div>
          <div className="people-foot" onMouseDown={(e) => e.preventDefault()}>
            <span aria-live="polite">
              {visible.length > MAX_ROWS
                ? `Showing ${MAX_ROWS} of ${visible.length}${maybeMore ? '+' : ''}: type to narrow it down`
                : searching
                  ? 'Searching…'
                  : `${selected.length} selected · ${visible.length} ${term ? 'found' : 'available'}${maybeMore && !term ? '+' : ''}`}
            </span>
            <span className="people-actions">
              {shown.length > 0 && (
                <button
                  type="button"
                  onClick={() => {
                    setPicked((p) => ({ ...p, ...Object.fromEntries(shown.map((u) => [u.id, u])) }))
                    onChange(
                      allShownChosen
                        ? selected.filter((id) => !shown.some((u) => u.id === id))
                        : [...new Set([...selected, ...shown.map((u) => u.id)])],
                    )
                  }}
                >
                  {allShownChosen ? 'Unselect shown' : term ? 'Select shown' : 'Select all'}
                </button>
              )}
              {selected.length > 0 && (
                <button type="button" onClick={() => onChange([])}>
                  Clear
                </button>
              )}
            </span>
          </div>
        </div>
      </div>
    </div>
  )
}
