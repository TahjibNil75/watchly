// Mirrors CheckType in app/monitoring/websites/models.py. A site's type is
// fixed once it is created; a ping check keeps its host in `url`, a DNS check
// its domain, a mail check `host:port`. `short` is what a badge shows.
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
    value: 'mail',
    label: 'Mail (SMTP)',
    short: 'Mail',
    description: 'Log in to an SMTP server such as SES or Resend, and watch its sending quota.',
  },
]

export const checkType = (value) => CHECK_TYPES.find((t) => t.value === value) ?? CHECK_TYPES[0]

export const isPing = (site) => site?.check_type === 'ping'
export const isDns = (site) => site?.check_type === 'dns'
export const isMail = (site) => site?.check_type === 'mail'

// Mirrors MailProvider. `host` is the SMTP server a new check is filled in
// with (SES's names its region), `username` its SMTP user when fixed.
export const MAIL_PROVIDERS = [
  { value: 'resend', label: 'Resend', host: () => 'smtp.resend.com:587', username: 'resend' },
  {
    value: 'ses',
    label: 'Amazon SES',
    host: (region) => `email-smtp.${region || 'us-east-1'}.amazonaws.com:587`,
    username: null,
  },
  { value: 'smtp', label: 'Other SMTP server', host: () => '', username: null },
]

export const mailProvider = (value) =>
  MAIL_PROVIDERS.find((p) => p.value === value) ?? MAIL_PROVIDERS[2]

// The regions Amazon SES sends from, as its console lists them. A region
// outside this list (a new one) can still be kept: see MailFields.
export const SES_REGIONS = [
  ['us-east-1', 'US East (N. Virginia)'],
  ['us-east-2', 'US East (Ohio)'],
  ['us-west-1', 'US West (N. California)'],
  ['us-west-2', 'US West (Oregon)'],
  ['ca-central-1', 'Canada (Central)'],
  ['ca-west-1', 'Canada West (Calgary)'],
  ['sa-east-1', 'South America (São Paulo)'],
  ['eu-west-1', 'Europe (Ireland)'],
  ['eu-west-2', 'Europe (London)'],
  ['eu-west-3', 'Europe (Paris)'],
  ['eu-central-1', 'Europe (Frankfurt)'],
  ['eu-central-2', 'Europe (Zurich)'],
  ['eu-north-1', 'Europe (Stockholm)'],
  ['eu-south-1', 'Europe (Milan)'],
  ['il-central-1', 'Israel (Tel Aviv)'],
  ['me-south-1', 'Middle East (Bahrain)'],
  ['me-central-1', 'Middle East (UAE)'],
  ['af-south-1', 'Africa (Cape Town)'],
  ['ap-south-1', 'Asia Pacific (Mumbai)'],
  ['ap-south-2', 'Asia Pacific (Hyderabad)'],
  ['ap-southeast-1', 'Asia Pacific (Singapore)'],
  ['ap-southeast-2', 'Asia Pacific (Sydney)'],
  ['ap-southeast-3', 'Asia Pacific (Jakarta)'],
  ['ap-southeast-5', 'Asia Pacific (Malaysia)'],
  ['ap-northeast-1', 'Asia Pacific (Tokyo)'],
  ['ap-northeast-2', 'Asia Pacific (Seoul)'],
  ['ap-northeast-3', 'Asia Pacific (Osaka)'],
]

// SES's SMTP host names its region: `email-smtp.eu-west-1.amazonaws.com:587`.
const SES_HOST = /^email-smtp(?:-fips)?\.([a-z]{2}(?:-[a-z]+)+-\d)\.amazonaws\.com(?::\d+)?$/i

// The region an SES SMTP endpoint names, or null for any other host.
export const sesHostRegion = (url) => SES_HOST.exec(url.trim())?.[1].toLowerCase() ?? null

// The same endpoint in another region, or null when `url` is not an SES host.
export const sesHostIn = (url, region) =>
  sesHostRegion(url) ? url.trim().replace(/(email-smtp(?:-fips)?\.)[^.]+/i, `$1${region}`) : null

// Mirrors MailTls.
export const MAIL_TLS = [
  { value: 'starttls', label: 'STARTTLS (port 587)' },
  { value: 'implicit', label: 'Implicit TLS (port 465)' },
  { value: 'none', label: 'None (port 25, no login)' },
]

// What a quota window is called: SES counts a rolling 24 hours.
export const QUOTA_PERIODS = { '24h': 'Last 24 hours', day: 'Today', month: 'This month' }

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
