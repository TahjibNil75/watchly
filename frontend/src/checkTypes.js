// Mirrors CheckType in app/monitoring/websites/models.py. A site's type is
// fixed once it is created; a ping check keeps its host in `url`, a DNS check
// its domain. `short` is what a badge shows.
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
  {
    value: 'dns',
    label: 'DNS records',
    short: 'DNS',
    description: 'Watch a domain’s records at several resolvers for failures and changes.',
  },
]

export const checkType = (value) => CHECK_TYPES.find((t) => t.value === value) ?? CHECK_TYPES[0]

export const isPing = (site) => site?.check_type === 'ping'
export const isDns = (site) => site?.check_type === 'dns'

// Mirrors DnsRecordType. `example` is what an expected value looks like.
export const RECORD_TYPES = [
  { value: 'A', label: 'A (IPv4 address)', example: '203.0.113.10' },
  { value: 'AAAA', label: 'AAAA (IPv6 address)', example: '2001:db8::10' },
  { value: 'CNAME', label: 'CNAME (alias)', example: 'example.net' },
  { value: 'MX', label: 'MX (mail server)', example: '10 mx1.example.com' },
  { value: 'TXT', label: 'TXT (text, e.g. SPF)', example: 'v=spf1 include:_spf.example.com ~all' },
]

export const recordType = (value) =>
  RECORD_TYPES.find((t) => t.value === value) ?? RECORD_TYPES[0]

// TXT values are quoted: they may hold commas and spaces.
export const recordText = (type, value) => (type === 'TXT' ? `"${value}"` : value)

// Round trips come as fractional milliseconds: "0.42 ms", "12.3 ms", "184 ms".
export function rtt(ms) {
  if (ms == null) return '—'
  if (ms < 10) return `${ms.toFixed(2)} ms`
  if (ms < 100) return `${ms.toFixed(1)} ms`
  return `${Math.round(ms)} ms`
}
