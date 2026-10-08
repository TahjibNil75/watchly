import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import {
  ConfirmDialog,
  EnvironmentBadge,
  ErrorBanner,
  Loading,
  PersonList,
  UserChecklist,
} from '../components.jsx'
import { dateTime, percent, since, timeAgo } from '../format.js'
import InfraCheckForm from '../InfraCheckForm.jsx'
import { CheckTypeBadge, DiagnoseSteps, HealthBadge, KindBadge, KindIcon } from '../Infra.jsx'
import {
  LB_TYPES,
  PROBLEMS,
  every,
  kindInfo,
  resourceTrouble,
  snapshotLine,
  stateLabel,
  targetReason,
  targetState,
} from '../infra.js'
import Maintenance from '../Maintenance.jsx'
import { ENVIRONMENTS } from '../environments.js'
import { canCreateProjects, canManageProject } from '../roles.js'
import { useApi } from '../useApi.js'

const RESULTS_SHOWN = 30
const RANGES = ['24h', '7d', '30d', '90d']
const MAINTENANCE_ACTIONS = {
  start: api.startResourceMaintenance,
  end: api.endResourceMaintenance,
  cancel: api.cancelResourceMaintenance,
}

// The hero's tone, in the dashboard's colours.
const TONES = {
  healthy: 'up',
  degraded: 'pending',
  down: 'down',
  unknown: 'pending',
  maintenance: 'maintenance',
  paused: 'paused',
  missing: 'down',
}

// What the resource is in AWS, as its details say.
function awsNoun(r) {
  const d = r.aws_detail ?? {}
  if (r.kind === 'load_balancer') return `${d.scheme ?? ''} ${LB_TYPES[d.type] ?? 'load balancer'}`.trim()
  if (r.kind === 'auto_scaling_group') return 'EC2 Auto Scaling group'
  if (r.kind === 'database') return `${d.cluster ? 'Aurora' : 'RDS'} ${d.engine ?? ''} database`.replace('  ', ' ')
  return `${d.public_ip ? 'public' : 'private'} EC2 instance`
}

function StatusLine({ resource: r }) {
  switch (r.state) {
    case 'down':
      return (
        <>
          <strong>Down</strong> for {since(r.down_since)} · since {dateTime(r.down_since)}
        </>
      )
    case 'degraded':
      return (
        <>
          <strong>Degraded</strong> for {since(r.degraded_since)}: up, with a problem
        </>
      )
    case 'healthy':
      return (
        <>
          <strong>Healthy</strong> · every check passed {timeAgo(r.last_checked_at)}
        </>
      )
    case 'missing':
      return (
        <>
          <strong>Missing</strong> since {dateTime(r.missing_since)}: AWS no longer lists it, so its checks are stopped
        </>
      )
    case 'maintenance':
      return <strong>In maintenance: not checked, nobody alerted</strong>
    case 'paused':
      return <strong>Paused: not checked</strong>
    default:
      return <strong>Not checked yet</strong>
  }
}

function Hero({ resource: r, children }) {
  const tone = TONES[r.state] ?? 'pending'
  const trouble = resourceTrouble(r)
  return (
    <section className={`site-hero tone-${tone}`}>
      <div className="site-hero-head">
        <span className={`site-hero-icon${tone === 'down' ? ' is-alarm' : ''}`}>
          <KindIcon kind={r.kind} />
        </span>
        <div className="site-hero-title">
          <h1>
            {r.name}
            <EnvironmentBadge environment={r.environment} />
            <KindBadge kind={r.kind} />
          </h1>
          <p className="site-hero-url muted">
            {r.kind === 'auto_scaling_group' ? (
              <>
                min {r.aws_detail?.min_size} · desired {r.aws_detail?.desired_capacity} · max {r.aws_detail?.max_size}
              </>
            ) : (
              <code>
                {r.address ?? '—'}
                {r.kind === 'database' && r.aws_detail?.port ? `:${r.aws_detail.port}` : ''}
              </code>
            )}
            {r.aws_detail?.public_ip && (
              <>
                {' '}
                · public <code>{r.aws_detail.public_ip}</code>
              </>
            )}{' '}
            · {awsNoun(r)} <code>{r.aws_id.split('/').slice(-2).join('/')}</code>
          </p>
        </div>
        {children && <div className="actions">{children}</div>}
      </div>
      <div className="site-hero-body">
        <p className="site-hero-status">
          <StatusLine resource={r} />
        </p>
        <p className="site-hero-tally muted small">
          {r.checks.length} check{r.checks.length === 1 ? '' : 's'} · AWS says <strong>{r.aws_state ?? '—'}</strong>
          {r.synced_at && <> · read {timeAgo(r.synced_at)}</>}
        </p>
      </div>
      {trouble && r.state !== 'healthy' && <p className="site-hero-error">{trouble}</p>}
    </section>
  )
}

function settingsLine(check) {
  const s = check.settings
  const via =
    (s.min_healthy_instances ? ` · at least ${s.min_healthy_instances} instances` : '') +
    (s.use_public_ip ? ' · public IP' : '')
  switch (check.check_type) {
    case 'ping':
      return `${s.count} pings${via}`
    case 'tcp':
      return [`port ${s.port}`, s.tls && 'TLS', s.expect_banner && `banner “${s.expect_banner}”`]
        .filter(Boolean)
        .join(' · ') + via
    case 'http':
      return `${s.scheme.toUpperCase()} ${s.port ?? (s.scheme === 'https' ? 443 : 80)} ${s.path} → ${s.expected_status}${via}`
    case 'target_health':
      return `at least ${s.min_healthy_targets} healthy`
    case 'group_health':
      return `at least ${s.min_healthy_instances} healthy`
    default:
      return ''
  }
}

function CheckRow({ resource, check, canManage, onChange, onError }) {
  const [editing, setEditing] = useState(false)
  const [diagnosis, setDiagnosis] = useState(null)
  const [busy, setBusy] = useState(false)

  async function run(action) {
    setBusy(true)
    onError(null)
    try {
      await action()
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  const diagnose = () =>
    run(async () => setDiagnosis(await api.infraDiagnose({ resource_id: resource.id, check_id: check.id })))
  const toggle = () => run(async () => onChange(await api.updateCheck(resource.id, check.id, { is_enabled: !check.is_enabled })))
  const remove = () => {
    if (!window.confirm(`Remove the check “${check.name}”? Its history goes with it.`)) return
    run(async () => onChange(await api.deleteCheck(resource.id, check.id)))
  }

  const snapshot = snapshotLine(check)
  return (
    <li className={`check-item${check.is_enabled ? '' : ' is-off'}`}>
      <div className="check-item-head">
        <HealthBadge health={check.is_enabled ? check.health : 'paused'} />
        <CheckTypeBadge type={check.check_type} />
        <strong>{check.name}</strong>
        <span className="muted small">
          {settingsLine(check)} · {every(check.check_interval_seconds)}
        </span>
        {canManage && (
          <span className="check-item-actions">
            <button type="button" className="btn btn-sm" onClick={diagnose} disabled={busy}>
              {busy && !editing ? 'Running…' : check.health === 'down' ? 'Why is this down?' : 'Diagnose'}
            </button>
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => setEditing(!editing)}>
              {editing ? 'Close' : 'Edit'}
            </button>
            <button type="button" className="btn btn-sm btn-ghost" onClick={toggle} disabled={busy}>
              {check.is_enabled ? 'Turn off' : 'Turn on'}
            </button>
            <button type="button" className="btn btn-sm btn-ghost" onClick={remove} disabled={busy || resource.checks.length <= 1}>
              Remove
            </button>
          </span>
        )}
      </div>
      <div className="check-item-body small">
        {check.last_result ? (
          <span className={check.last_result.ok ? '' : 'text-down'}>
            {check.last_result.summary}
            <span className="muted"> · {timeAgo(check.last_checked_at)}</span>
          </span>
        ) : (
          <span className="muted">Not checked yet</span>
        )}
        {snapshot && <span className="muted"> · {snapshot}</span>}
        {check.problems.map((p) => (
          <div key={p.kind} className="text-pending">
            {PROBLEMS[p.kind] ?? p.kind}: {p.detail} · since {dateTime(p.since)}
          </div>
        ))}
      </div>
      {diagnosis && <DiagnoseSteps result={diagnosis} />}
      {editing && (
        <InfraCheckForm
          resource={resource}
          check={check}
          onCancel={() => setEditing(false)}
          onSubmit={async (payload) => {
            onChange(await api.updateCheck(resource.id, check.id, payload))
            setEditing(false)
          }}
        />
      )}
    </li>
  )
}

function Targets({ resourceId }) {
  const targets = useApi(() => api.resourceTargets(resourceId), [resourceId], { pollMs: 60000 })
  if (targets.loading) return <Loading />
  const groups = targets.data?.target_groups ?? []
  return (
    <section className="card">
      <h2>Targets</h2>
      <p className="muted small">
        What the load balancer&apos;s own health check says about each target, with AWS&apos;s reason code, and
        when that changed over the last 24 hours.
      </p>
      <ErrorBanner error={targets.error} />
      {!groups.length && <p className="muted">No target health check yet.</p>}
      {groups.map((g) => (
        <div key={g.arn} className="target-group">
          <h3>
            {g.name} <span className="muted small">{g.health_check}</span>
          </h3>
          {!g.targets.length ? (
            <p className="muted small">No results in the last 24 hours.</p>
          ) : (
            <div className="target-grid">
              {g.targets.map((t) => {
                const state = targetState(t.state)
                const reason = targetReason(t.reason)
                return (
                  <div key={t.id} className={`target tone-${state.tone}`}>
                    <div className="target-head">
                      <strong>{t.name ?? t.id}</strong>
                      <span className={`badge badge-${state.badge}`}>{state.label}</span>
                    </div>
                    <div className="muted small">
                      {t.address ?? t.id}
                      {t.port ? `:${t.port}` : ''}
                      {t.az ? ` · ${t.az}` : ''}
                    </div>
                    {state.note && <div className="muted small">{state.note}</div>}
                    {reason && (
                      <div className={`small ${state.failing ? 'text-down' : 'muted'}`} title={t.reason}>
                        {reason}
                        {t.description && t.description !== reason ? `: ${t.description}` : ''}
                      </div>
                    )}
                    {t.since && <div className="muted small">since {dateTime(t.since)}</div>}
                    {t.timeline.length > 1 && (
                      <div className="muted small">{t.timeline.length - 1} change(s) in 24 h</div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      ))}
    </section>
  )
}

// An Auto Scaling group's instances: as the group saw them at the last sync,
// and how each fared in the latest run of each check that runs on them.
function Instances({ resource: r }) {
  const fanned = r.checks.filter((c) => c.is_enabled && Array.isArray(c.snapshot?.instances))
  const synced = r.aws_detail?.instances ?? []
  const ids = [...new Set([...synced.map((i) => i.id), ...fanned.flatMap((c) => c.snapshot.instances.map((i) => i.id))])]
  const byId = Object.fromEntries(synced.map((i) => [i.id, i]))
  const results = Object.fromEntries(
    fanned.map((c) => [c.id, Object.fromEntries(c.snapshot.instances.map((i) => [i.id, i]))]),
  )
  return (
    <section className="card">
      <h2>Instances</h2>
      <p className="muted small">
        Lifecycle and health as the group reported them {r.synced_at ? timeAgo(r.synced_at) : 'at the last sync'};
        each check&apos;s column is its latest run. New instances are checked once they are past the group&apos;s
        grace period ({r.aws_detail?.health_check_grace_period ?? 0} s).
      </p>
      {!ids.length ? (
        <p className="muted">No instances.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Instance</th>
                <th>Address</th>
                <th>Lifecycle</th>
                <th>Group health</th>
                {fanned.map((c) => (
                  <th key={c.id}>{c.name}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {ids.map((id) => {
                const i = byId[id] ?? {}
                return (
                  <tr key={id}>
                    <td className="record nowrap">
                      {id}
                      {i.az && <div className="muted small">{i.az}</div>}
                    </td>
                    <td className="record nowrap">
                      {i.private_ip ?? '—'}
                      {i.public_ip && <div className="muted small">public {i.public_ip}</div>}
                    </td>
                    <td>{i.lifecycle_state ?? <span className="muted">new since sync</span>}</td>
                    <td className={i.health_status && i.health_status !== 'Healthy' ? 'text-down' : undefined}>
                      {i.health_status ?? '—'}
                    </td>
                    {fanned.map((c) => {
                      const x = results[c.id][id]
                      return (
                        <td key={c.id} className={x && !x.ok ? 'text-down small' : 'small'}>
                          {x ? (
                            <>
                              {x.ok ? '✓' : '✗'} {x.summary}
                            </>
                          ) : (
                            <span className="muted">not checked</span>
                          )}
                        </td>
                      )
                    })}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function Stats({ resource }) {
  const [range, setRange] = useState('24h')
  const stats = useApi(() => api.resourceStats(resource.id, range), [resource.id, range], { pollMs: 60000 })
  const metricLabel = {
    packet_loss_percent: 'Packet loss',
    healthy_targets: 'Healthy targets',
    healthy_instances: 'Healthy instances',
  }
  const metricUnit = { packet_loss_percent: '%', healthy_targets: '', healthy_instances: '' }
  const counted = new Set(['healthy_targets', 'healthy_instances'])
  return (
    <section className="card">
      <div className="section-head-row">
        <h2>History</h2>
        <div className="segmented" role="group" aria-label="Range">
          {RANGES.map((r) => (
            <button key={r} type="button" aria-pressed={range === r} onClick={() => setRange(r)}>
              {r}
            </button>
          ))}
        </div>
      </div>
      <ErrorBanner error={stats.error} />
      {stats.loading ? (
        <Loading />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Check</th>
                <th>Uptime</th>
                <th className="num">Avg</th>
                <th className="num">Max</th>
                <th>Its own figure</th>
                <th>Over time</th>
              </tr>
            </thead>
            <tbody>
              {(stats.data?.checks ?? []).map((c) => (
                <tr key={c.check_id}>
                  <td>
                    <CheckTypeBadge type={c.check_type} /> {c.name}
                  </td>
                  <td className="nowrap">{c.checks ? percent(c.uptime_percent) : '—'}</td>
                  <td className="num">{c.avg_response_ms != null ? `${c.avg_response_ms} ms` : '—'}</td>
                  <td className="num">{c.max_response_ms != null ? `${c.max_response_ms} ms` : '—'}</td>
                  <td className="nowrap">
                    {c.metric && c.metric_max != null
                      ? `${metricLabel[c.metric]} ${counted.has(c.metric) ? `${c.metric_min}–${c.metric_max}` : `≤ ${c.metric_max}`}${metricUnit[c.metric]}`
                      : '—'}
                  </td>
                  <td>
                    <span className="mini-strip" aria-hidden="true">
                      {c.series.map((b) => (
                        <i
                          key={b.start}
                          className={!b.checks ? 'is-empty' : b.uptime_percent === 100 ? 'is-up' : b.uptime_percent >= 99 ? 'is-slow' : 'is-down'}
                          title={`${dateTime(b.start)}: ${b.checks ? percent(b.uptime_percent) : 'no checks'}`}
                        />
                      ))}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

// A server launched by an Auto Scaling group is replaced when the group
// scales, and only the group's own checks follow its instances: say so while
// the group is not monitored in this VPC.
function UnwatchedGroup({ resource: r, canAdd }) {
  const name = r.kind === 'server' ? r.aws_detail?.auto_scaling_group : null
  const groups = useApi(
    () => (name ? api.listResources({ vpc_id: r.vpc.id, kind: 'auto_scaling_group' }) : null),
    [name, r.vpc.id],
  )
  if (!name || !groups.data || groups.data.items.some((g) => g.aws_id === name)) return null
  return (
    <div className="banner banner-info banner-action">
      <span>
        {r.name} belongs to Auto Scaling group <strong>{name}</strong>, which is not monitored. When the group
        scales, this instance is replaced and these checks stop following it. Monitor the group to check
        every instance it runs, its capacity against desired, and to be told when it scales out or in.
      </span>
      {canAdd && (
        <Link
          to={`/infra/new?project=${r.project.id}&vpc=${r.vpc.id}&pick=${encodeURIComponent(`asg:${name}`)}`}
          className="btn btn-sm"
        >
          Monitor the group
        </Link>
      )}
    </div>
  )
}

const gradeTone = (grade) => ({ A: 'up', B: 'up', C: 'unknown', D: 'unknown' })[grade] ?? 'down'

const EDGE_STATUS = {
  ok: { tone: 'up', label: 'OK' },
  weak: { tone: 'unknown', label: 'Weak' },
  missing: { tone: 'down', label: 'Missing' },
  unknown: { tone: 'paused', label: 'Unknown' },
  info: { tone: 'paused', label: 'N/A' },
}

// What a Web ACL rule does with a match: a rule group's own rules decide
// ("enforce") unless the whole group was set to Count.
const RULE_ACTION = {
  enforce: { tone: 'up', label: 'Enforce' },
  block: { tone: 'up', label: 'Block' },
  count: { tone: 'unknown', label: 'Count' },
  allow: { tone: 'paused', label: 'Allow' },
  captcha: { tone: 'paused', label: 'CAPTCHA' },
  challenge: { tone: 'paused', label: 'Challenge' },
}

function ruleText(rule) {
  if (rule.kind === 'managed') return `${rule.vendor}/${rule.group}${rule.version ? ` ${rule.version}` : ''}`
  if (rule.kind === 'group') return `rule group ${rule.group}`
  if (rule.kind === 'rate') return `rate limit ${rule.limit}`
  if (rule.kind === 'geo') return 'geo match'
  if (rule.kind === 'ip_set') return 'IP set'
  return 'custom rule'
}

function EdgeList({ items }) {
  return (
    <ul className="sec-list">
      {items.map((item) => {
        const status = EDGE_STATUS[item.status]
        return (
          <li key={item.key}>
            <span className={`badge badge-${status.tone}`}>{status.label}</span>
            <div className="sec-body">
              <strong>{item.label}</strong>
              {item.value && (
                <code className="sec-value" title={item.value}>
                  {item.value}
                </code>
              )}
              {item.note && <span className="muted small">{item.note}</span>}
            </div>
          </li>
        )
      })}
    </ul>
  )
}

// The AWS WAF Web ACL in front of an ALB and the ALB's own hardening, graded
// by the API: what is there, and what is missing.
function EdgeSecurity({ resource: r }) {
  const report = r.edge_security
  if (r.kind !== 'load_balancer' || r.aws_detail?.type !== 'application') return null
  const rules = r.aws_detail?.waf?.acl?.rules ?? []
  return (
    <section className="card">
      <div className="card-head">
        <h2>WAF &amp; edge security</h2>
        {report && report.total > 0 && (
          <span className={`badge badge-${gradeTone(report.grade)} sec-grade`}>
            {report.grade} · {report.score} of {report.total}
          </span>
        )}
      </div>
      {!report ? (
        <p className="muted small">Read from AWS at the next sync.</p>
      ) : (
        <>
          <h3 className="sec-extras-title">AWS WAF{report.acl_name && ` · ${report.acl_name}`}</h3>
          <EdgeList items={report.items.filter((i) => i.group === 'waf')} />
          <h3 className="sec-extras-title">Load balancer</h3>
          <EdgeList items={report.items.filter((i) => i.group === 'alb')} />
          {report.extras?.length > 0 && (
            <>
              <h3 className="sec-extras-title">Also set, not graded</h3>
              <ul className="sec-list">
                {report.extras.map((x, i) => (
                  <li key={`${x.label}-${i}`}>
                    <div className="sec-body">
                      <strong>{x.label}</strong>
                      <code className="sec-value" title={x.value}>
                        {x.value}
                      </code>
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
          {rules.length > 0 && (
            <details className="advanced sec-foot">
              <summary>All {rules.length} Web ACL rules</summary>
              <ul className="sec-list">
                {rules.map((rule) => {
                  const action = RULE_ACTION[rule.action] ?? { tone: 'paused', label: rule.action ?? '—' }
                  return (
                    <li key={`${rule.priority}-${rule.name}`}>
                      <span className={`badge badge-${action.tone}`}>{action.label}</span>
                      <div className="sec-body">
                        <strong>
                          {rule.priority}. {rule.name}
                        </strong>
                        <code className="sec-value">{ruleText(rule)}</code>
                        {rule.count_override === 'partial' && (
                          <span className="muted small">Counting only: {rule.counted_rules.join(', ')}</span>
                        )}
                      </div>
                    </li>
                  )
                })}
              </ul>
            </details>
          )}
        </>
      )}
    </section>
  )
}

function AwsDetails({ resource: r }) {
  const d = r.aws_detail ?? {}
  const rows = [
    ['AWS id', r.aws_id],
    ['ARN', r.aws_arn !== r.aws_id && r.aws_arn],
    ['State', r.aws_state],
    ['Instance type', d.instance_type],
    ['Engine', d.engine && `${d.engine} ${d.engine_version ?? ''}`.trim()],
    ['Instance class', d.instance_class],
    ['Endpoint', d.endpoint && `${d.endpoint}${d.port ? `:${d.port}` : ''}`],
    ['Multi-AZ', r.kind === 'database' && (d.multi_az ? `yes, standby in ${d.secondary_az ?? 'another AZ'}` : 'no')],
    ['Publicly accessible', r.kind === 'database' && (d.publicly_accessible ? 'yes' : 'no')],
    [
      'Storage',
      d.allocated_storage_gb &&
        `${d.allocated_storage_gb} GB ${d.storage_type ?? ''}`.trim() +
          (d.max_allocated_storage_gb ? `, grows to ${d.max_allocated_storage_gb} GB` : ''),
    ],
    ['Aurora cluster', d.cluster],
    ['Replica of', d.replica_of],
    ['Read replicas', d.replicas?.join(', ')],
    ['Subnet group', d.subnet_group],
    ['Type', d.type && (d.type === 'network' ? 'Network Load Balancer' : 'Application Load Balancer')],
    ['Scheme', d.scheme],
    ['Private IP', d.private_ip],
    ['Public IP', r.kind === 'server' && (d.public_ip ?? 'none')],
    ['Public DNS', d.public_dns],
    [
      'Capacity',
      r.kind === 'auto_scaling_group' && `min ${d.min_size} · desired ${d.desired_capacity} · max ${d.max_size}`,
    ],
    [
      'Health check',
      d.health_check_type && `${d.health_check_type}, grace period ${d.health_check_grace_period ?? 0} s`,
    ],
    ['Launch template', d.launch_template],
    ['Subnets', d.subnets?.join(', ')],
    ['Availability zone', d.az ?? d.azs?.join(', ')],
    ['Subnet', d.subnet],
    ['Listeners', d.listeners?.map((l) => `${l.protocol}:${l.port}`).join(', ')],
    ['Web ACL', d.waf && (d.waf.acl ? d.waf.acl.name : d.waf.error ? 'not readable' : 'none')],
    [
      'Target groups',
      d.target_groups?.length > 0 &&
        d.target_groups.map((g) => (
          <div key={g.arn ?? g.name}>
            {g.name}
            {g.health_check && <span className="muted small"> · health check {g.health_check}</span>}
          </div>
        )),
    ],
    ['Auto Scaling group', d.auto_scaling_group],
    ['Security groups', d.security_groups?.join(', ')],
    ['VPC', r.vpc.name],
    ['Read from AWS', r.synced_at && dateTime(r.synced_at)],
  ].filter(([, value]) => value)
  const tags = Object.entries(d.tags ?? {})
  return (
    <section className="card">
      <h2>In AWS</h2>
      <dl className="kv">
        {rows.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd className={label === 'ARN' || label === 'AWS id' ? 'record' : undefined}>{value}</dd>
          </div>
        ))}
        {tags.length > 0 && (
          <div className="kv-stack">
            <dt>Tags</dt>
            <dd>
              {tags.map(([k, v]) => (
                <span key={k} className="chip">
                  {k}={v}
                </span>
              ))}
            </dd>
          </div>
        )}
      </dl>
    </section>
  )
}

function Results({ resource }) {
  const results = useApi(() => api.resourceResults(resource.id, { limit: RESULTS_SHOWN }), [resource.id, resource.last_checked_at])
  const names = Object.fromEntries(resource.checks.map((c) => [c.id, c]))
  return (
    <section className="card">
      <h2>Recent results</h2>
      <ErrorBanner error={results.error} />
      {results.loading ? (
        <Loading />
      ) : !results.data?.items.length ? (
        <p className="muted">No results yet.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Check</th>
                <th>Result</th>
                <th className="num">Time</th>
                <th>Address</th>
              </tr>
            </thead>
            <tbody>
              {results.data.items.map((x) => (
                <tr key={x.id}>
                  <td className="nowrap" title={dateTime(x.checked_at)}>
                    {timeAgo(x.checked_at)}
                  </td>
                  <td className="nowrap">{names[x.check_id]?.name ?? `#${x.check_id}`}</td>
                  <td className={x.ok ? '' : 'text-down'}>
                    {x.summary}
                    {x.error_type && <span className="muted small"> · {x.error_type}</span>}
                  </td>
                  <td className="num nowrap">{x.response_time_ms != null ? `${x.response_time_ms} ms` : '—'}</td>
                  <td className="record nowrap">{x.address ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

// Which environment this resource belongs to, as a website's form has it.
function EnvironmentCard({ resource: r, canManage, onChange, onError }) {
  const [value, setValue] = useState(r.environment ?? '')
  const [busy, setBusy] = useState(false)
  async function save(e) {
    e.preventDefault()
    setBusy(true)
    onError(null)
    try {
      onChange(await api.updateResource(r.id, { environment: value || null }))
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }
  return (
    <section className="card">
      <h2>Environment</h2>
      <form className="form" onSubmit={save}>
        <label className="field">
          <span>Environment</span>
          <select value={value} onChange={(e) => setValue(e.target.value)} disabled={!canManage || busy}>
            <option value="">Not set</option>
            {ENVIRONMENTS.map((env) => (
              <option key={env.value} value={env.value}>
                {env.label}
              </option>
            ))}
          </select>
        </label>
        {canManage && (
          <div className="form-actions">
            <button type="submit" className="btn btn-sm" disabled={busy || value === (r.environment ?? '')}>
              {busy ? 'Saving…' : 'Save environment'}
            </button>
          </div>
        )}
      </form>
    </section>
  )
}

const NO_ENDPOINT = { scheme: 'http', port: '', path: '/health', expected_status: 200, use_public_ip: false }

// An Auto Scaling group's opt-in notifications: servers added, servers removed,
// and an optional endpoint probed on each new server.
function ScalingNotifications({ resource: r, canManage, onChange, onError }) {
  const saved = r.scale_health_check
  const [probe, setProbe] = useState(Boolean(saved))
  const [endpoint, setEndpoint] = useState(saved ? { ...saved, port: saved.port ?? '' } : NO_ENDPOINT)
  const [busy, setBusy] = useState(false)

  async function save(payload) {
    setBusy(true)
    onError(null)
    try {
      onChange(await api.updateResource(r.id, payload))
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }
  const saveEndpoint = (e) => {
    e.preventDefault()
    save({
      scale_health_check: probe
        ? { ...endpoint, port: endpoint.port === '' ? null : Number(endpoint.port), expected_status: Number(endpoint.expected_status) }
        : null,
    })
  }
  const setField = (key) => (e) =>
    setEndpoint({ ...endpoint, [key]: e.target.type === 'checkbox' ? e.target.checked : e.target.value })

  return (
    <section className="card">
      <h2>Scaling notifications</h2>
      <p className="muted small">
        Optional, and off until you turn them on. Watchly compares the group&apos;s servers at each sync (about every
        few minutes), so a server that comes and goes between two syncs is not reported.
      </p>
      <label className="field field-check">
        <input
          type="checkbox"
          checked={r.notify_scale_out}
          disabled={!canManage || busy}
          onChange={(e) => save({ notify_scale_out: e.target.checked })}
        />
        <span>Tell me when servers are added (scale out)</span>
      </label>
      <label className="field field-check">
        <input
          type="checkbox"
          checked={r.notify_scale_in}
          disabled={!canManage || busy}
          onChange={(e) => save({ notify_scale_in: e.target.checked })}
        />
        <span>Tell me when servers are removed (scale in)</span>
      </label>

      <form className="form" onSubmit={saveEndpoint}>
        <label className="field field-check">
          <input type="checkbox" checked={probe} disabled={!canManage || busy} onChange={(e) => setProbe(e.target.checked)} />
          <span>Check a health endpoint on each new server</span>
        </label>
        <p className="muted small">
          The scale-out message then says whether the new server answered. It applies only to scale-out
          notifications.
        </p>
        {probe && (
          <div className="check-fields">
            <label className="field">
              <span>Scheme</span>
              <select value={endpoint.scheme} onChange={setField('scheme')} disabled={!canManage}>
                <option>http</option>
                <option>https</option>
              </select>
            </label>
            <label className="field">
              <span>Port</span>
              <input type="number" min={1} max={65535} value={endpoint.port} onChange={setField('port')} placeholder="80 or 443" disabled={!canManage} />
            </label>
            <label className="field">
              <span>Path</span>
              <input value={endpoint.path} onChange={setField('path')} maxLength={1024} required disabled={!canManage} />
            </label>
            <label className="field">
              <span>Expected status</span>
              <input type="number" min={100} max={599} value={endpoint.expected_status} onChange={setField('expected_status')} disabled={!canManage} />
            </label>
            <label className="field field-check">
              <input type="checkbox" checked={endpoint.use_public_ip} onChange={setField('use_public_ip')} disabled={!canManage} />
              <span>Use the server&apos;s public IP</span>
            </label>
          </div>
        )}
        {canManage && (
          <div className="form-actions">
            <button type="submit" className="btn btn-sm" disabled={busy || (!probe && !saved)}>
              {busy ? 'Saving…' : probe ? 'Save endpoint' : 'Remove endpoint'}
            </button>
          </div>
        )}
      </form>
    </section>
  )
}

export default function InfraResourceDetail() {
  const { id } = useParams()
  const { user } = useAuth()
  const navigate = useNavigate()
  const [adding, setAdding] = useState([])
  const [addingCheck, setAddingCheck] = useState(false)
  const [notice, setNotice] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [confirmingDelete, setConfirmingDelete] = useState(false)

  const resource = useApi(() => api.getResource(id), [id], { pollMs: 30000 })
  const projectId = resource.data?.project.id
  const project = useApi(() => (projectId ? api.getProject(projectId).catch(() => null) : null), [projectId])
  const canManage = canManageProject(user, project.data)
  const users = useApi(() => (canManage ? api.listUsers({ is_active: true }) : null), [canManage])

  if (resource.loading) return <Loading />
  if (!resource.data) {
    return (
      <>
        <ErrorBanner error={resource.error} />
        <Link to="/infra">← Back to infrastructure</Link>
      </>
    )
  }
  const r = resource.data

  async function run(action) {
    setBusy(true)
    setActionError(null)
    setNotice(null)
    try {
      await action()
    } catch (err) {
      setActionError(err)
    } finally {
      setBusy(false)
    }
  }

  const checkNow = () =>
    run(async () => {
      const out = await api.checkResource(r.id)
      resource.setData(out.resource)
      const failed = out.results.filter((x) => !x.ok)
      setNotice(
        `Checked just now: ${out.results.length - failed.length} of ${out.results.length} passed · ${stateLabel(out.resource.state).toLowerCase()}` +
          (out.alert_sent ? ` · “${out.alert_sent}” alert sent` : ''),
      )
    })
  const toggleEnabled = () => run(async () => resource.setData(await api.updateResource(r.id, { is_enabled: !r.is_enabled })))
  const remove = () =>
    run(async () => {
      await api.deleteResource(r.id)
      navigate('/infra')
    })
  const addRecipients = () =>
    run(async () => {
      resource.setData(await api.addResourceRecipients(r.id, adding))
      setAdding([])
    })
  const removeRecipient = (person) => run(async () => resource.setData(await api.removeResourceRecipient(r.id, person.id)))

  const recipientIds = new Set(r.extra_recipients.map((u) => u.id))
  const candidates = (users.data?.items ?? []).filter((u) => !recipientIds.has(u.id))
  const canCheck = r.is_enabled && !r.maintenance && r.state !== 'missing'

  return (
    <>
      <p className="crumbs">
        <Link to="/infra">Infrastructure</Link>
        {' / '}
        <Link to={`/infra/vpcs/${r.vpc.id}`}>{r.vpc.name}</Link>
        {' / '}
        <Link to={`/projects/${r.project.id}`}>{r.project.name}</Link>
      </p>
      <Hero resource={r}>
        {canManage && (
          <>
            <button type="button" className="btn btn-primary" onClick={checkNow} disabled={busy || !canCheck}>
              {busy ? 'Working…' : 'Check now'}
            </button>
            <button type="button" className="btn btn-purple" onClick={toggleEnabled} disabled={busy}>
              {r.is_enabled ? 'Pause' : 'Resume'}
            </button>
          </>
        )}
      </Hero>

      <ErrorBanner error={actionError ?? resource.error} />
      {notice && <div className="banner banner-info">{notice}</div>}
      <UnwatchedGroup resource={r} canAdd={canManage && canCreateProjects(user)} />

      <div className="site-body">
        <section className="card">
          <div className="section-head-row">
            <h2>Checks</h2>
            {canManage && !addingCheck && (
              <button type="button" className="btn btn-sm" onClick={() => setAddingCheck(true)}>
                Add check
              </button>
            )}
          </div>
          <p className="muted small">
            The {kindInfo(r.kind).noun} is down when any of these is down, and healthy when every one
            passes: one alert per outage, listing what failed.
          </p>
          {addingCheck && (
            <InfraCheckForm
              resource={r}
              onCancel={() => setAddingCheck(false)}
              onSubmit={async (payload) => {
                resource.setData(await api.addCheck(r.id, payload))
                setAddingCheck(false)
              }}
            />
          )}
          <ul className="check-list">
            {r.checks.map((c) => (
              <CheckRow
                key={c.id}
                resource={r}
                check={c}
                canManage={canManage}
                onChange={resource.setData}
                onError={setActionError}
              />
            ))}
          </ul>
        </section>

        <EnvironmentCard resource={r} canManage={canManage} onChange={resource.setData} onError={setActionError} />

        {r.kind === 'auto_scaling_group' && <Instances resource={r} />}

        {r.kind === 'auto_scaling_group' && (
          <ScalingNotifications resource={r} canManage={canManage} onChange={resource.setData} onError={setActionError} />
        )}

        {(r.kind === 'load_balancer' ||
          (r.kind === 'auto_scaling_group' && r.checks.some((c) => c.check_type === 'target_health'))) && (
          <Targets resourceId={r.id} />
        )}

        <Stats resource={r} />

        <EdgeSecurity resource={r} />

        <div className="site-row site-pair">
          <AwsDetails resource={r} />
          <Maintenance
            site={r}
            canManage={canManage}
            onChange={resource.setData}
            onBoundary={resource.reload}
            noun={kindInfo(r.kind).noun}
            actions={MAINTENANCE_ACTIONS}
          />
        </div>

        <section className="card">
          <h2>Who is alerted</h2>
          <p className="muted small">
            {r.project.name}&apos;s members and extra emails, and its Slack, Telegram and WhatsApp, as for its
            websites. Add people who should hear about this resource only:
          </p>
          <PersonList
            people={r.extra_recipients}
            onRemove={canManage ? removeRecipient : null}
            emptyLabel="No extra recipients."
          />
          {canManage && candidates.length > 0 && (
            <>
              <UserChecklist users={candidates} selected={adding} onChange={setAdding} />
              <button type="button" className="btn btn-sm" onClick={addRecipients} disabled={busy || !adding.length}>
                Add {adding.length || ''} recipient{adding.length === 1 ? '' : 's'}
              </button>
            </>
          )}
        </section>

        <Results resource={r} />

        {canManage && (
          <section className="card card-danger">
            <h2>Stop monitoring</h2>
            <p className="muted small">
              Deletes {r.name}&apos;s checks and their history from Watchly. Nothing changes in AWS.
            </p>
            <button type="button" className="btn btn-danger-solid" onClick={() => setConfirmingDelete(true)} disabled={busy}>
              Delete
            </button>
          </section>
        )}
        {confirmingDelete && (
          <ConfirmDialog
            title={`Stop monitoring ${r.name}?`}
            busy={busy}
            error={actionError}
            onConfirm={remove}
            onCancel={() => setConfirmingDelete(false)}
          >
            Deletes its checks and their history from Watchly. Nothing changes in AWS.
          </ConfirmDialog>
        )}
      </div>
    </>
  )
}
