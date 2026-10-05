// Mirrors CheckType in app/monitoring/websites/models.py. A site's type is
// fixed once it is created; a ping check keeps its host in `url`, a DNS check
// its domain, and a database check `host:port`. `short` is what a badge shows.
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
  {
    value: 'database',
    label: 'Database',
    short: 'DB',
    description: 'Check a database answers at its endpoint. No user or password needed.',
  },
]

export const checkType = (value) => CHECK_TYPES.find((t) => t.value === value) ?? CHECK_TYPES[0]

export const isPing = (site) => site?.check_type === 'ping'
export const isDns = (site) => site?.check_type === 'dns'
export const isDatabase = (site) => site?.check_type === 'database'

// Mirrors DbEngine and db_probe.DEFAULT_PORTS. `tlsFromStart` engines cannot
// switch to TLS mid-connection, so a check says whether to use it; the others
// use it whenever the server offers it. `schemes` are the connection URL
// schemes that name each.
export const DB_ENGINES = [
  {
    value: 'postgresql',
    label: 'PostgreSQL',
    covers: 'Aurora PostgreSQL, RDS',
    port: 5432,
    schemes: ['postgres', 'postgresql'],
  },
  {
    value: 'mysql',
    label: 'MySQL / MariaDB',
    covers: 'Aurora MySQL, RDS',
    port: 3306,
    schemes: ['mysql', 'mariadb'],
  },
  {
    value: 'redis',
    label: 'Redis / Valkey',
    covers: 'ElastiCache, MemoryDB',
    port: 6379,
    tlsFromStart: true,
    schemes: ['redis', 'rediss', 'valkey', 'valkeys'],
  },
  {
    value: 'mongodb',
    label: 'MongoDB',
    covers: 'DocumentDB, Atlas',
    port: 27017,
    tlsFromStart: true,
    schemes: ['mongodb'],
  },
]

export const dbEngine = (value) => DB_ENGINES.find((e) => e.value === value) ?? DB_ENGINES[0]

// `postgres://user:secret@db.example.com:5432/app` → the engine it names, the
// endpoint (`db.example.com:5432`), whether it asks for TLS from the start
// (`rediss://`), and whether it held credentials, which are dropped here and
// never sent. Null when `text` is not a connection URL Watchly knows.
export function parseConnectionUrl(text) {
  const match = /^([a-z][a-z\d+.-]*):\/\/(.*)$/i.exec(text.trim())
  if (!match) return null
  const scheme = match[1].toLowerCase()
  const engine = DB_ENGINES.find((e) => e.schemes.includes(scheme))
  if (!engine) return null
  let authority = match[2].split(/[/?#]/)[0]
  const hadCredentials = authority.includes('@')
  authority = authority.slice(authority.lastIndexOf('@') + 1)
  return {
    engine: engine.value,
    endpoint: authority.split(',')[0],
    tls: scheme === 'rediss' || scheme === 'valkeys' || /[?&](tls|ssl)=true/i.test(text),
    hadCredentials,
  }
}

// What a database check's server said, for a check row: role or state.
export const dbSaid = (database) =>
  database ? database.state ?? database.message ?? null : null

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
