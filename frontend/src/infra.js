// What the infrastructure pages share that is not a component: labels, the
// check types each kind takes, and small helpers. Mirrors app/monitoring/infra/aws.
import { useEffect, useState } from 'react'
import { api } from './api.js'
import { duration } from './format.js'

// The clouds Watchly reads infrastructure from. Only AWS is wired up; the
// others show in the picker as coming soon so the flow is provider-shaped.
export const PROVIDERS = [
  { value: 'aws', label: 'AWS', mark: 'AWS', note: 'EC2, Auto Scaling, load balancers and RDS', available: true },
  { value: 'gcp', label: 'Google Cloud', mark: 'GCP', note: 'Compute Engine, load balancing and Cloud SQL', available: false },
]

// `noun` and `nouns` are for the middle of a sentence.
export const KINDS = [
  {
    value: 'server',
    label: 'Server',
    plural: 'Servers',
    noun: 'server',
    nouns: 'servers',
    aws: 'EC2 instance, in a public or private subnet',
  },
  {
    value: 'load_balancer',
    label: 'Load balancer',
    plural: 'Load balancers',
    noun: 'load balancer',
    nouns: 'load balancers',
    aws: 'Application or Network Load Balancer, internal or internet-facing',
  },
  {
    value: 'auto_scaling_group',
    label: 'Auto Scaling group',
    plural: 'Auto Scaling groups',
    noun: 'Auto Scaling group',
    nouns: 'Auto Scaling groups',
    aws: 'EC2 Auto Scaling group: its own health, and every instance it has in service',
  },
  {
    value: 'database',
    label: 'Database',
    plural: 'Databases',
    noun: 'database',
    nouns: 'databases',
    aws: 'RDS DB instance, Aurora included: watched without logging in',
  },
]
export const kindInfo = (value) =>
  KINDS.find((k) => k.value === value) ?? { value, label: value, plural: value, noun: value, nouns: value }

export const CHECK_TYPES = {
  ping: { label: 'Ping', note: 'ICMP echo to the instance' },
  tcp: { label: 'TCP', note: 'Does a port accept connections' },
  http: { label: 'HTTP', note: 'GET a path; for a load balancer, through its listener' },
  target_health: { label: 'Target health', note: "The load balancer's own verdict for each target" },
  group_health: {
    label: 'Group health',
    note: "The Auto Scaling group's own view: instances healthy and in service, against its desired capacity",
  },
  db_status: { label: 'DB status', note: 'What RDS says of the database: available, stopped, storage full…' },
  db_metrics: {
    label: 'DB metrics',
    note: 'CloudWatch CPU, free storage and memory, connections and replica lag, against thresholds',
  },
}

// Which checks each kind of resource takes, as the API allows. An Auto
// Scaling group's ping, tcp and http run on every instance it has in service. A
// database's checks never log in: its port, and what RDS and CloudWatch say.
export const CHECKS_BY_KIND = {
  server: ['ping', 'tcp', 'http'],
  load_balancer: ['http', 'tcp', 'target_health'],
  auto_scaling_group: ['http', 'group_health', 'tcp', 'ping', 'target_health'],
  database: ['db_status', 'tcp', 'db_metrics'],
}

// The checks that can go to a server's public IP instead of its private one.
export const PUBLIC_IP_CHECKS = new Set(['ping', 'tcp', 'http'])

export const LB_TYPES = { application: 'ALB', network: 'NLB' }

// Whether the internet reaches it: a server with a public IP, an
// internet-facing load balancer, a group with an instance that has one, or a
// publicly accessible database.
export function isPublic(resource) {
  const d = resource.aws_detail ?? {}
  if (resource.kind === 'database') return Boolean(d.publicly_accessible)
  if (resource.kind === 'load_balancer') return d.scheme === 'internet-facing'
  if (resource.kind === 'auto_scaling_group') return (d.instances ?? []).some((i) => i.public_ip)
  return Boolean(d.public_ip)
}

// "ALB · internet-facing", "public 203.0.113.25", "private", "2–6, 3 desired",
// "postgres · db.t4g.medium".
export function exposureLine(resource) {
  const d = resource.aws_detail ?? {}
  if (resource.kind === 'database') return [d.engine, d.instance_class, d.multi_az && 'Multi-AZ'].filter(Boolean).join(' · ')
  if (resource.kind === 'load_balancer') return [LB_TYPES[d.type] ?? d.type, d.scheme].filter(Boolean).join(' · ')
  if (resource.kind === 'auto_scaling_group') return `${d.min_size}–${d.max_size}, ${d.desired_capacity} desired`
  return d.public_ip ? `public ${d.public_ip}` : 'private'
}

// A resource's state, in the order the dashboard counts them.
export const STATES = [
  { key: 'healthy', label: 'Healthy', note: 'Every check passes' },
  { key: 'degraded', label: 'Degraded', note: 'Up, with a problem' },
  { key: 'down', label: 'Down', note: 'A check is failing' },
  { key: 'unknown', label: 'Pending', note: 'Not checked yet' },
  { key: 'maintenance', label: 'Maintenance', note: 'Checks on hold' },
  { key: 'paused', label: 'Paused', note: 'Checks off' },
  { key: 'missing', label: 'Missing', note: 'Gone from AWS' },
]
export const stateLabel = (key) => STATES.find((s) => s.key === key)?.label ?? key

export const PROBLEMS = {
  slow_response: 'Slow response',
  packet_loss: 'Packet loss',
  targets_unhealthy: 'Unhealthy targets',
  instances_failing: 'Instances failing their check',
  instances_unhealthy: 'Unhealthy instances',
  capacity_short: 'Short of desired capacity',
  db_busy: 'Database busy',
  replication_broken: 'Replication broken',
  high_cpu: 'High CPU',
  low_storage: 'Low free storage',
  low_memory: 'Low freeable memory',
  many_connections: 'Many connections',
  replica_lag: 'Replica lag',
}

// --- services ---------------------------------------------------------------
// A service is what the dashboard's cards stand for: the load balancer, Auto
// Scaling group and servers that serve one thing. The API has no such record,
// so it is worked out here from what the resources already carry.

const SERVICE_TAGS = ['Service', 'service']

const serviceTag = (resource) => {
  const tags = resource.aws_detail?.tags ?? {}
  for (const key of SERVICE_TAGS) {
    const value = typeof tags[key] === 'string' ? tags[key].trim() : ''
    if (value) return value
  }
  return null
}

// The most used value, the first alphabetically on a tie.
const mostCommon = (values) => {
  const counts = new Map()
  for (const v of values) counts.set(v, (counts.get(v) ?? 0) + 1)
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0]?.[0] ?? null
}

// The state a group of resources is shown in: the worst one that matters.
export function groupState(counts) {
  for (const key of ['down', 'degraded', 'missing', 'unknown']) if (counts[key]) return key
  if (counts.healthy) return 'healthy'
  return counts.maintenance ? 'maintenance' : 'paused'
}

export function stateCounts(resources) {
  const counts = { total: resources.length }
  for (const s of STATES) counts[s.key] = 0
  for (const r of resources) counts[r.state] += 1
  return counts
}

// "1 down · 2 degraded", or the state's name when nothing is wrong.
export function groupSummary(counts) {
  const trouble = ['down', 'degraded', 'missing'].filter((k) => counts[k]).map((k) => `${counts[k]} ${stateLabel(k).toLowerCase()}`)
  return trouble.length ? trouble.join(' · ') : stateLabel(groupState(counts))
}

// Resources into services, one VPC at a time:
// - a load balancer and an Auto Scaling group that share a target group stay
//   together, and a server belongs with the group it runs in;
// - a `Service` tag names the service, and joins every part carrying it;
// - a group without a tag is named after its Auto Scaling group, else its
//   load balancer;
// - servers with neither a tag nor a group are listed as ungrouped.
// Worst services first.
export function groupServices(resources) {
  const byVpc = new Map()
  for (const r of resources) byVpc.set(r.vpc.id, [...(byVpc.get(r.vpc.id) ?? []), r])

  const services = []
  for (const items of byVpc.values()) {
    const parent = new Map(items.map((r) => [r.id, r.id]))
    const find = (id) => {
      while (parent.get(id) !== id) {
        parent.set(id, parent.get(parent.get(id)))
        id = parent.get(id)
      }
      return id
    }
    const join = (a, b) => parent.set(find(a), find(b))

    const groupsByName = new Map(items.filter((r) => r.kind === 'auto_scaling_group').map((r) => [r.aws_id, r]))
    const firstWithTargetGroup = new Map()
    for (const r of items) {
      if (r.kind === 'server') {
        const group = groupsByName.get(r.aws_detail?.auto_scaling_group)
        if (group) join(r.id, group.id)
        continue
      }
      for (const tg of r.aws_detail?.target_groups ?? []) {
        const first = firstWithTargetGroup.get(tg.arn)
        if (first === undefined) firstWithTargetGroup.set(tg.arn, r.id)
        else join(r.id, first)
      }
    }

    const parts = new Map()
    for (const r of items) parts.set(find(r.id), [...(parts.get(find(r.id)) ?? []), r])

    const named = new Map()
    for (const members of parts.values()) {
      const tag = mostCommon(members.map(serviceTag).filter(Boolean))
      const head = members.find((r) => r.kind === 'auto_scaling_group') ?? members.find((r) => r.kind === 'load_balancer')
      let key, name
      if (tag) [key, name] = [`tag:${tag.toLowerCase()}`, tag]
      else if (head) [key, name] = [`part:${head.id}`, head.name]
      else [key, name] = ['ungrouped', null]
      const service = named.get(key) ?? { id: `${members[0].vpc.id}:${key}`, name, tagged: Boolean(tag), vpc: members[0].vpc, resources: [] }
      service.resources.push(...members)
      named.set(key, service)
    }
    services.push(...named.values())
  }

  const rank = ['down', 'degraded', 'missing', 'unknown', 'healthy', 'maintenance', 'paused']
  return services
    .map((s) => {
      const resources = [...s.resources].sort((a, b) => a.name.localeCompare(b.name))
      const counts = stateCounts(resources)
      return { ...s, resources, counts, state: groupState(counts) }
    })
    .sort(
      (a, b) =>
        rank.indexOf(a.state) - rank.indexOf(b.state) ||
        (a.name === null) - (b.name === null) ||
        (a.name ?? '').localeCompare(b.name ?? '') ||
        a.vpc.name.localeCompare(b.vpc.name),
    )
}

// An Auto Scaling group's instances, each in the state its checks give it:
// the last run of its ping, tcp or http check says if it passed, else AWS's
// own word on it. Only the failing ones carry a note.
export function groupInstances(resource) {
  const ran = new Map()
  for (const check of resource.checks) {
    if (!check.is_enabled) continue
    for (const row of check.snapshot?.instances ?? []) {
      if (row.id && (!ran.has(row.id) || row.ok === false)) ran.set(row.id, row)
    }
  }
  return (resource.aws_detail?.instances ?? []).map((m) => {
    const row = ran.get(m.id)
    if (m.lifecycle_state !== 'InService') return { id: m.id, state: 'unknown', note: m.lifecycle_state }
    if (row) return row.ok ? { id: m.id, state: 'healthy' } : { id: m.id, state: 'down', note: row.summary }
    return m.health_status === 'Healthy' ? { id: m.id, state: 'healthy' } : { id: m.id, state: 'down', note: 'Unhealthy in AWS' }
  })
}

// Every resource in the scope, a page of 200 at a time.
export async function loadAllResources(query) {
  const items = []
  for (let offset = 0; offset < 2000; offset += 200) {
    const page = await api.listResources({ ...query, sort: 'state', limit: 200, offset })
    items.push(...page.items)
    if (items.length >= page.total || !page.items.length) break
  }
  return items
}

// Whether the API has infrastructure monitoring on; asked once per page load.
let enabledOnce = null
export function useInfraEnabled() {
  const [enabled, setEnabled] = useState(null)
  useEffect(() => {
    enabledOnce ??= api
      .health()
      .then((health) => health.infra_aws === 'on')
      .catch(() => false)
    let live = true
    enabledOnce.then((value) => live && setEnabled(value))
    return () => {
      live = false
    }
  }, [])
  return enabled
}

// What is wrong, in one line: failing checks, else open problems.
export function resourceTrouble(resource) {
  const failing = resource.checks
    .filter((c) => c.is_enabled && c.health === 'down')
    .map((c) => `${c.name}: ${c.last_result?.summary ?? 'failed'}`)
  if (failing.length) return failing.join('; ')
  if (resource.problems.length) return resource.problems.map((p) => p.detail ?? PROBLEMS[p.kind]).join('; ')
  if (resource.state === 'missing') return 'AWS no longer lists it; its checks are stopped.'
  return null
}

// What the load balancer says of a target, in AWS's words. Only unhealthy and
// unavailable are failures; initial, draining and unused are normal passages
// that a check counts as neither healthy nor failing, so they are not red.
const TARGET_STATES = {
  healthy: { label: 'Healthy', tone: 'up', badge: 'healthy' },
  unhealthy: { label: 'Unhealthy', tone: 'down', badge: 'down', failing: true },
  unavailable: { label: 'Unavailable', tone: 'down', badge: 'down', failing: true },
  initial: {
    label: 'Registering',
    tone: 'pending',
    badge: 'degraded',
    note: 'Being registered: the load balancer is running its first health checks.',
  },
  draining: {
    label: 'Draining',
    tone: 'pending',
    badge: 'degraded',
    note: 'Leaving the group: no new requests, finishing the ones in flight until the deregistration delay ends.',
  },
  unused: {
    label: 'Not in use',
    tone: 'pending',
    badge: 'unknown',
    note: 'Not receiving traffic: its target group is not attached to a listener, or its Availability Zone is not enabled.',
  },
}

export function targetState(state) {
  return TARGET_STATES[state] ?? { label: state ?? 'Unknown', tone: 'pending', badge: 'unknown' }
}

// AWS's reason codes, in a few words; unknown ones are shown as sent.
const TARGET_REASONS = {
  'Target.DeregistrationInProgress': 'Deregistering',
  'Target.NotRegistered': 'Not registered',
  'Target.NotInUse': 'Target group has no listener',
  'Target.InvalidState': 'Instance stopped or terminated',
  'Target.IpUnusable': 'IP address cannot be used as a target',
  'Target.ResponseCodeMismatch': 'Health check got an unexpected status code',
  'Target.Timeout': 'Health check timed out',
  'Target.FailedHealthChecks': 'Health checks failed',
  'Target.HealthCheckDisabled': 'Health checks are off',
  'Elb.InitialHealthChecking': 'First health checks running',
  'Elb.RegistrationInProgress': 'Registration in progress',
  'Elb.InternalError': 'AWS-side error',
}

export function targetReason(reason) {
  return reason ? TARGET_REASONS[reason] ?? reason : null
}

// "2 of 3 targets healthy" and the like, from a check's latest figures.
export function snapshotLine(check) {
  const s = check.snapshot ?? {}
  // An Auto Scaling group's check, run on each of its instances.
  if (Array.isArray(s.instances) && check.check_type !== 'group_health') {
    if (s.total == null) return null
    return `${s.healthy} of ${s.total} instances pass${s.warming ? ` · ${s.warming} warming up` : ''}`
  }
  switch (check.check_type) {
    case 'ping':
      return s.avg_ms != null ? `${s.avg_ms.toFixed(1)} ms · ${s.loss_percent}% loss` : null
    case 'tcp':
      return s.port ? `port ${s.port} · ${s.connect_ms} ms` : null
    case 'http':
      return s.status_code ? `HTTP ${s.status_code} · ${s.response_ms} ms` : null
    case 'target_health':
      return s.total != null ? `${s.healthy} of ${s.total} targets healthy` : null
    case 'group_health':
      return s.desired != null
        ? `${s.healthy} of ${s.desired} desired healthy · ${s.in_service} in service${s.launching ? ` · ${s.launching} launching` : ''}`
        : null
    case 'db_status':
      return s.status ? [s.status, s.engine && `${s.engine} ${s.engine_version ?? ''}`.trim()].filter(Boolean).join(' · ') : null
    case 'db_metrics': {
      const parts = [
        s.cpu_percent != null && `CPU ${Math.round(s.cpu_percent)}%`,
        s.free_storage_gb != null && `${s.free_storage_gb.toFixed(1)} GB free`,
        s.freeable_memory_mb != null && `${Math.round(s.freeable_memory_mb)} MB memory`,
        s.connections != null && `${Math.round(s.connections)} connections`,
        s.replica_lag_seconds != null && `lag ${Math.round(s.replica_lag_seconds)} s`,
      ].filter(Boolean)
      return parts.length ? parts.join(' · ') : null
    }
    default:
      return null
  }
}

export const every = (seconds) => `every ${duration(seconds)}`

// The commercial AWS regions, for the region pickers. A value an account
// already holds that is not here (a newer region) is kept as its own option.
export const AWS_REGIONS = [
  {
    group: 'United States',
    regions: [
      ['us-east-1', 'N. Virginia'],
      ['us-east-2', 'Ohio'],
      ['us-west-1', 'N. California'],
      ['us-west-2', 'Oregon'],
    ],
  },
  {
    group: 'Canada and Mexico',
    regions: [
      ['ca-central-1', 'Canada Central'],
      ['ca-west-1', 'Calgary'],
      ['mx-central-1', 'Mexico'],
    ],
  },
  {
    group: 'Europe',
    regions: [
      ['eu-central-1', 'Frankfurt'],
      ['eu-central-2', 'Zurich'],
      ['eu-west-1', 'Ireland'],
      ['eu-west-2', 'London'],
      ['eu-west-3', 'Paris'],
      ['eu-north-1', 'Stockholm'],
      ['eu-south-1', 'Milan'],
      ['eu-south-2', 'Spain'],
    ],
  },
  {
    group: 'Asia Pacific',
    regions: [
      ['ap-south-1', 'Mumbai'],
      ['ap-south-2', 'Hyderabad'],
      ['ap-southeast-1', 'Singapore'],
      ['ap-southeast-2', 'Sydney'],
      ['ap-southeast-3', 'Jakarta'],
      ['ap-southeast-4', 'Melbourne'],
      ['ap-southeast-5', 'Malaysia'],
      ['ap-southeast-7', 'Thailand'],
      ['ap-northeast-1', 'Tokyo'],
      ['ap-northeast-2', 'Seoul'],
      ['ap-northeast-3', 'Osaka'],
      ['ap-east-1', 'Hong Kong'],
    ],
  },
  {
    group: 'Middle East and Africa',
    regions: [
      ['il-central-1', 'Tel Aviv'],
      ['me-south-1', 'Bahrain'],
      ['me-central-1', 'UAE'],
      ['af-south-1', 'Cape Town'],
    ],
  },
  { group: 'South America', regions: [['sa-east-1', 'São Paulo']] },
]

// An AWS account as the forms hold it (AwsAccounts.jsx, ProjectForm.jsx).
export const BLANK_ACCOUNT = {
  name: '',
  description: '',
  auth_type: 'access_key',
  access_key_id: '',
  secret_access_key: '',
  role_arn: '',
  external_id: '',
  default_region: '',
  environment: '',
  watch_deployments: false,
}

// "Access key AKIA…, assumes watchly-monitor", "Watchly's own credentials".
export function credentialsLine(a) {
  const base = a.auth_type === 'access_key' ? `Access key ${a.access_key_id}` : "Watchly's own credentials"
  return a.role_arn ? `${base}, assumes ${a.role_arn.split(':role/')[1] ?? a.role_arn}` : base
}

// A new account as the API takes it: blanks left out, no key with `default`.
export function newAccountPayload(form) {
  const payload = {}
  for (const key of Object.keys(BLANK_ACCOUNT)) {
    if (typeof BLANK_ACCOUNT[key] === 'boolean') {
      if (form[key]) payload[key] = true
      continue
    }
    const value = form[key].trim()
    if (form.auth_type !== 'access_key' && (key === 'access_key_id' || key === 'secret_access_key')) continue
    if (value) payload[key] = value
  }
  return payload
}
