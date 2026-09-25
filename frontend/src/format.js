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

export function timeAgo(iso) {
  if (!iso) return 'never'
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  return seconds < 5 ? 'just now' : `${duration(seconds)} ago`
}

export function dateTime(iso) {
  return iso ? new Date(iso).toLocaleString() : '—'
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
