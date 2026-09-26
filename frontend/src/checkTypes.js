// Mirrors CheckType in app/monitoring/websites/models.py. A site's type is
// fixed once it is created; a ping check keeps its host in `url`.
// `short` is what a badge shows.
export const CHECK_TYPES = [
  {
    value: 'http',
    label: 'HTTP(S)',
    short: 'HTTP',
    description: 'Request a URL and check the response.',
  },
  {
    value: 'ping',
    label: 'Ping (ICMP)',
    short: 'Ping',
    description: 'Check a server or network device answers at the IP level.',
  },
]

export const checkType = (value) => CHECK_TYPES.find((t) => t.value === value) ?? CHECK_TYPES[0]

export const isPing = (site) => site?.check_type === 'ping'

// Round trips come as fractional milliseconds: "0.42 ms", "12.3 ms", "184 ms".
export function rtt(ms) {
  if (ms == null) return '—'
  if (ms < 10) return `${ms.toFixed(2)} ms`
  if (ms < 100) return `${ms.toFixed(1)} ms`
  return `${Math.round(ms)} ms`
}
