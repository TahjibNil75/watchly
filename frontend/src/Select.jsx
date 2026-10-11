import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'

// A drop-in for <select> whose list opens inline, in the page's flow, rather
// than as a floating menu: a bordered box with the value on top that expands
// downward. It takes the same props and <option>/<optgroup> children, and
// calls onChange with an event-shaped { target: { value, name } }.
//
// The children render into a visually hidden native <select>. That keeps the
// form's own validation (`required`) and submission working, and the options
// are read back from it, so any way of writing the children is understood.

function readOptions(select) {
  const read = (o) => ({ value: o.value, label: o.textContent, disabled: o.disabled })
  const groups = []
  let loose = null
  for (const child of select.children) {
    if (child.tagName === 'OPTGROUP') {
      loose = null
      groups.push({ label: child.label, options: [...child.children].map(read) })
    } else if (child.tagName === 'OPTION') {
      if (!loose) groups.push((loose = { label: null, options: [] }))
      loose.options.push(read(child))
    }
  }
  return { groups, current: select.value }
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)

export default function Select({
  value,
  onChange,
  children,
  className = '',
  disabled,
  required,
  name,
  prefix,
  ...rest
}) {
  const uid = useId()
  const root = useRef(null)
  const native = useRef(null)
  const list = useRef(null)
  const trigger = useRef(null)
  const typed = useRef({ text: '', at: 0 })
  const [open, setOpen] = useState(false)
  const [model, setModel] = useState({ groups: [], current: '' })

  // Runs after every render, as the options are the children's and could change
  // on any of them; setState bails out unless they did.
  useLayoutEffect(() => {
    const next = readOptions(native.current)
    setModel((prev) => (same(prev, next) ? prev : next))
  })

  const flat = model.groups.flatMap((g) => g.options)
  const selected = flat.find((o) => o.value === model.current)
  const shown = open && !disabled

  const options = () => [...list.current.querySelectorAll('[role="option"]:not([aria-disabled="true"])')]

  const close = (refocus) => {
    setOpen(false)
    if (refocus) trigger.current?.focus()
  }

  const choose = (next) => {
    close(true)
    if (next !== model.current) onChange?.({ target: { value: next, name }, currentTarget: { value: next, name } })
  }

  // Opening puts focus on the chosen row, so the arrow keys work straight away.
  useEffect(() => {
    if (!open) return
    const items = options()
    const target = items.find((el) => el.getAttribute('aria-selected') === 'true') ?? items[0]
    if (!target) return
    target.focus({ preventScroll: true })
    const box = list.current
    box.scrollTop = target.offsetTop - box.clientHeight / 2 + target.offsetHeight / 2
  }, [open])

  // A click anywhere else closes it; Tab and focus moving away do too.
  useEffect(() => {
    if (!open) return
    const away = (e) => {
      if (!root.current?.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', away)
    return () => document.removeEventListener('mousedown', away)
  }, [open])

  const move = (to) => {
    const items = options()
    const at = items.indexOf(document.activeElement)
    const next = to === 'first' ? 0 : to === 'last' ? items.length - 1 : (at + to + items.length) % items.length
    items[next]?.focus()
  }

  const onListKey = (e) => {
    if (e.key === 'ArrowDown') move(1)
    else if (e.key === 'ArrowUp') move(-1)
    else if (e.key === 'Home') move('first')
    else if (e.key === 'End') move('last')
    else if (e.key === 'Escape') close(true)
    else if (e.key === 'Tab') close(false)
    else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      // Type-ahead: letters typed in quick succession narrow to a prefix.
      const now = e.timeStamp
      const t = typed.current
      t.text = now - t.at > 700 ? e.key.toLowerCase() : t.text + e.key.toLowerCase()
      t.at = now
      const items = options()
      const hit = items.find((el) => el.textContent.trim().toLowerCase().startsWith(t.text))
      if (hit) hit.focus()
      else return
    } else return
    e.preventDefault()
  }

  const onTriggerKey = (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      setOpen(true)
    } else if (e.key === 'Escape' && open) {
      e.preventDefault()
      setOpen(false)
    }
  }

  const empty = !model.current
  const cls = ['acc-select', shown && 'is-open', disabled && 'is-disabled', className].filter(Boolean).join(' ')

  return (
    <div
      ref={root}
      className={cls}
      onBlur={(e) => {
        if (!root.current.contains(e.relatedTarget)) setOpen(false)
      }}
    >
      <button
        ref={trigger}
        type="button"
        className="acc-trigger"
        aria-haspopup="listbox"
        aria-expanded={shown}
        aria-controls={`${uid}-list`}
        disabled={disabled}
        onClick={() => setOpen(!open)}
        onKeyDown={onTriggerKey}
        {...rest}
      >
        {prefix && <span className="acc-prefix">{prefix}</span>}
        <span className={`acc-value${empty ? ' is-empty' : ''}`}>{selected?.label ?? ''}</span>
        <svg className="acc-chev" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="m5 8 5 5 5-5" />
        </svg>
      </button>
      <div className="acc-body" inert={!shown}>
        <div>
          <div
            ref={list}
            id={`${uid}-list`}
            className="acc-list"
            role="listbox"
            aria-label={rest['aria-label']}
            onKeyDown={onListKey}
            // Keeps focus where it is, so a click on a row is not a blur first.
            onMouseDown={(e) => e.preventDefault()}
          >
            {model.groups.map((g, i) => (
              <div key={g.label ?? `loose-${i}`} role={g.label ? 'group' : 'presentation'} aria-label={g.label ?? undefined}>
                {g.label && <div className="acc-group" aria-hidden="true">{g.label}</div>}
                {g.options.map((o) => (
                  <button
                    key={o.value}
                    type="button"
                    role="option"
                    tabIndex={-1}
                    className="acc-opt"
                    aria-selected={o.value === model.current}
                    aria-disabled={o.disabled || undefined}
                    onClick={() => !o.disabled && choose(o.value)}
                  >
                    <span className="acc-opt-label">{o.label}</span>
                    <svg className="acc-tick" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                      <path d="m4 10.5 4 4 8-9" />
                    </svg>
                  </button>
                ))}
              </div>
            ))}
          </div>
        </div>
      </div>
      <select
        ref={native}
        className="acc-native"
        value={value ?? ''}
        onChange={() => {}}
        required={required}
        disabled={disabled}
        name={name}
        tabIndex={-1}
        aria-hidden="true"
      >
        {children}
      </select>
    </div>
  )
}
