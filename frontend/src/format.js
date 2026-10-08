import { getPrefs, zoneOptions } from './prefs.js'

export function duration(totalSeconds) {
  const s = Math.max(0, Math.round(totalSeconds))
  const days = Math.floor(s / 86400)
  const hours = Math.floor((s % 86400) / 3600)
  const minutes = Math.floor((s % 3600) / 60)
  if (days) return hours ? `${days}d ${hours}h` : `${days}d`
  if (hours) return minutes ? `${hours}h ${minutes}m` : `${hours}h`
  if (minutes) return `${minutes}m`
  return `${s}s`
}

export function since(iso) {
  return duration((Date.now() - new Date(iso).getTime()) / 1000)
}

export function until(iso) {
  return duration((new Date(iso).getTime() - Date.now()) / 1000)
}

export function timeAgo(iso) {
  if (!iso) return 'never'
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  return seconds < 5 ? 'just now' : `${duration(seconds)} ago`
}

// 'Monstar People' -> 'MP', 'client' -> 'CL': for a person's or project's tile.
export function initials(name) {
  const words = name.trim().split(/\s+/).filter(Boolean)
  const letters = words.length > 1 ? words[0][0] + words[1][0] : (words[0] ?? '?').slice(0, 2)
  return letters.toUpperCase()
}

// Floored, so 99.996% never reads as a perfect 100%.
export const percent = (value) =>
  value == null ? '—' : value === 100 ? '100%' : `${(Math.floor(value * 100) / 100).toFixed(2)}%`

// Times follow the zone and clock chosen on the Profile page (see prefs.js).
export const timeFormat = (options) => new Intl.DateTimeFormat(undefined, { ...options, ...zoneOptions() })

export function dateTime(iso) {
  return iso
    ? new Date(iso).toLocaleString(undefined, zoneOptions())
    : '—'
}

export const timeOfDay = (iso) => timeFormat({ hour: '2-digit', minute: '2-digit' }).format(new Date(iso))

export const calendarDate = (iso) =>
  iso ? timeFormat({ day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(iso)) : '—'

// The calendar day an instant falls on in the chosen zone, to tell days apart.
export const dayKey = (iso) => timeFormat({ year: 'numeric', month: 'numeric', day: 'numeric' }).format(new Date(iso))

// The chosen zone's clock reading for an instant, as [y, m, d, h, mi, s].
function zonedParts(date) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-US', {
      timeZone: zoneOptions().timeZone,
      hourCycle: 'h23',
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
      hour: 'numeric',
      minute: 'numeric',
      second: 'numeric',
    })
      .formatToParts(date)
      .map((part) => [part.type, Number(part.value)]),
  )
  return [parts.year, parts.month, parts.day, parts.hour, parts.minute, parts.second]
}

// How far the chosen zone is ahead of UTC at an instant, in ms.
const zoneOffset = (date) => {
  const [y, m, d, h, mi, s] = zonedParts(date)
  return Date.UTC(y, m - 1, d, h, mi, s) - Math.floor(date.getTime() / 1000) * 1000
}

// A datetime-local input's value ('2026-10-09T14:30') read as the chosen
// zone's wall clock, as an ISO instant. The device's zone when none is chosen.
export function zonedInputToIso(local) {
  if (getPrefs().timeZone === 'auto') return new Date(local).toISOString()
  const [y, m, d, h, mi] = local.split(/[-T:]/).map(Number)
  const wall = Date.UTC(y, m - 1, d, h, mi)
  const first = wall - zoneOffset(new Date(wall))
  return new Date(wall - zoneOffset(new Date(first))).toISOString()
}

// Now in the chosen zone, as a datetime-local value.
export function zonedNowInput() {
  const now = new Date()
  if (getPrefs().timeZone === 'auto') {
    now.setSeconds(0, 0)
    return new Date(now.getTime() - now.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
  }
  const [y, m, d, h, mi] = zonedParts(now)
  const two = (n) => String(n).padStart(2, '0')
  return `${y}-${two(m)}-${two(d)}T${two(h)}:${two(mi)}`
}

// The month before this one, as 'YYYY-MM' in UTC, which is how reports cut months.
export function previousMonthUtc() {
  const now = new Date()
  const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1))
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`
}

// Accepts addresses separated by commas, semicolons, spaces or newlines.
export function parseEmails(text) {
  return text
    .split(/[\s,;]+/)
    .map((part) => part.trim())
    .filter(Boolean)
}

// Accepts phone numbers separated by commas, semicolons or newlines — not
// spaces, which people write inside a number. The API normalizes each one.
export function parsePhoneNumbers(text) {
  return text
    .split(/[,;\n]+/)
    .map((part) => part.trim())
    .filter(Boolean)
}

// A size in bytes as people read it: '512 B', '38.4 KB', '2.1 MB'.
export function bytes(n) {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}
