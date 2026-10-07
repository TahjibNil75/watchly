import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { Empty, ErrorBanner, Loading, PageHeader } from '../components.jsx'
import { ENVIRONMENTS } from '../environments.js'
import { KindIcon, ResourceTable } from '../Infra.jsx'
import { InfraMap } from '../NetworkMap.jsx'
import { KINDS, STATES, kindInfo, loadAllResources, useInfraEnabled } from '../infra.js'
import { canCreateProjects, canViewAllProjects } from '../roles.js'
import { useApi } from '../useApi.js'

const PAGE_SIZE = 50
// The map reads at most this many VPCs' topologies from AWS for its links.
const MAP_TOPOLOGIES = 8
const VIEW_KEY = 'watchly.infraView'
const VIEWS = [
  ['map', 'Map'],
  ['table', 'Table'],
]

// A filter shown as "Label  Value". The native select sits invisibly on top, so
// the keyboard, the phone picker and screen readers all work as they do for a select.
function FilterPill({ label, value, onChange, options, allLabel }) {
  const current = options.find((o) => o.value === value)
  return (
    <label className={`filter-pill${value ? ' is-active' : ''}`}>
      <span className="k">{label}</span>
      <span className="v">{current ? current.label : allLabel}</span>
      <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="m5 8 5 5 5-5" />
      </svg>
      <select value={value} onChange={(e) => onChange(e.target.value)} aria-label={label}>
        <option value="">{`${allLabel} (${label})`}</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  )
}

function savedView() {
  try {
    const saved = localStorage.getItem(VIEW_KEY)
    return VIEWS.some(([value]) => value === saved) ? saved : 'map'
  } catch {
    return 'map'
  }
}

// The cards: everything, then one per state, each filtering the table.
const FILTERS = [{ key: 'all', label: 'All resources', note: '' }, ...STATES]

function StateCard({ filter, counts, active, onPick }) {
  const n = filter.key === 'all' ? counts?.total : counts?.[filter.key]
  const alarm = filter.key === 'down' && n > 0
  const classes = ['stat', `stat-${filter.key}`, active && 'is-active', alarm && 'is-alarm']
  return (
    <button type="button" className={classes.filter(Boolean).join(' ')} onClick={onPick} aria-pressed={active}>
      <span className="stat-label">
        {alarm && <span className="stat-ping" />}
        {filter.label}
      </span>
      <span className="stat-main">
        <span className="stat-value">{n ?? '–'}</span>
      </span>
      <span className="stat-note">
        {filter.key === 'all'
          ? counts?.total
            ? `${counts.healthy} of ${counts.total} healthy`
            : 'Nothing watched yet'
          : filter.note}
      </span>
    </button>
  )
}

function VpcCard({ vpc }) {
  const c = vpc.counts
  const tone = vpc.health === 'unreachable' ? 'down' : c.down ? 'down' : c.degraded ? 'degraded' : vpc.health === 'untested' ? 'unknown' : 'healthy'
  return (
    <Link to={`/infra/vpcs/${vpc.id}`} className={`card vpc-card tone-${tone}`}>
      <div className="vpc-card-head">
        <strong>{vpc.name}</strong>
        <span className={`badge badge-${vpc.health === 'unreachable' ? 'down' : vpc.health === 'untested' ? 'unknown' : 'healthy'}`}>
          {vpc.health}
        </span>
      </div>
      <div className="muted small">{vpc.cidrs.join(', ')}</div>
      <div className="vpc-card-counts small">
        <span>{c.total} resources</span>
        {c.down > 0 && <span className="text-down">{c.down} down</span>}
        {c.degraded > 0 && <span className="text-pending">{c.degraded} degraded</span>}
        {c.missing > 0 && <span className="text-down">{c.missing} missing</span>}
      </div>
      {c.total > 0 && (
        <span className="stat-bar" aria-hidden="true">
          {['healthy', 'degraded', 'down', 'unknown', 'maintenance', 'paused', 'missing'].map(
            (s) => c[s] > 0 && <i key={s} className={`stat-bar-${s}`} style={{ flexGrow: c[s] }} />,
          )}
        </span>
      )}
    </Link>
  )
}

export default function Infrastructure() {
  const { user } = useAuth()
  const enabled = useInfraEnabled()
  const [filter, setFilter] = useState('all')
  const [kind, setKind] = useState('')
  const [vpcId, setVpcId] = useState('')
  const [environment, setEnvironment] = useState('')
  const [search, setSearch] = useState('')
  const [q, setQ] = useState('')
  const [page, setPage] = useState(0)
  const [view, setViewState] = useState(savedView)
  const setView = (next) => {
    setViewState(next)
    try {
      localStorage.setItem(VIEW_KEY, next)
    } catch {
      // Remembering the view is a nicety.
    }
  }

  useEffect(() => {
    const timer = setTimeout(() => {
      setQ(search.trim())
      setPage(0)
    }, 300)
    return () => clearTimeout(timer)
  }, [search])

  const scope = { kind, vpc_id: vpcId, q, environment }
  const overview = useApi(() => (enabled ? api.infraOverview({ vpc_id: vpcId, environment }) : null), [enabled, vpcId, environment], {
    pollMs: 30000,
  })
  const summary = useApi(() => (enabled ? api.resourceSummary(scope) : null), [enabled, kind, vpcId, q, environment], {
    pollMs: 30000,
  })
  // The map needs every resource of the VPC to draw it whole, so the search
  // and the filters pick resources client-side there.
  const whole = view === 'map'
  const everything = useApi(
    () => (enabled && whole ? loadAllResources({ vpc_id: vpcId, environment }) : null),
    [enabled, whole, vpcId, environment],
    { pollMs: 30000 },
  )
  // What AWS says links them (target groups, subnets), cached by the API.
  const vpcIds = overview.data?.vpcs.map((v) => v.id).join(',') ?? ''
  const topologies = useApi(
    () =>
      enabled && view === 'map' && vpcIds
        ? Promise.all(vpcIds.split(',').slice(0, MAP_TOPOLOGIES).map((vpc) => api.vpcTopology(vpc).catch(() => null)))
        : null,
    [enabled, view, vpcIds],
    { pollMs: 60000 },
  )
  const resources = useApi(
    () =>
      enabled && view === 'table'
        ? api.listResources({
            ...scope,
            state: filter === 'all' ? undefined : filter,
            sort: 'state',
            limit: PAGE_SIZE,
            offset: page * PAGE_SIZE,
          })
        : null,
    [enabled, view, filter, kind, vpcId, q, environment, page],
    { pollMs: 30000 },
  )

  if (enabled === null) return <Loading />
  if (!enabled) {
    return (
      <>
        <PageHeader title="Infrastructure" />
        <Empty>
          Infrastructure monitoring is off. Set <code>INFRA_AWS_ENABLED=true</code> on the API, run it on an
          EC2 instance with a read-only role, and EC2 servers, Auto Scaling groups and load balancers can be
          watched from here.
        </Empty>
      </>
    )
  }

  const creator = canCreateProjects(user)
  const manager = canViewAllProjects(user)
  const ov = overview.data
  const total = resources.data?.total ?? 0
  const lastPage = Math.max(Math.ceil(total / PAGE_SIZE) - 1, 0)
  if (resources.data && page > lastPage) setPage(lastPage)
  const pick = (setter) => (value) => {
    setter(value)
    setPage(0)
  }

  const nothingYet = ov && ov.counts.total === 0 && !vpcId
  const needle = q.toLowerCase()
  const has = (text) => text?.toLowerCase().includes(needle)
  const filtered = needle || filter !== 'all' || kind
  const matches = (r) =>
    (!needle || [r.name, r.aws_id, r.address].some(has)) && (filter === 'all' || r.state === filter) && (!kind || r.kind === kind)
  const emptyLabel = nothingYet ? (
    manager ? (
      <>
        Nothing is watched yet. <Link to="/infra/settings">Register a VPC</Link>, grant it to a project, then{' '}
        <Link to="/infra/new">add its resources</Link>.
      </>
    ) : creator ? (
      <>
        Nothing is watched yet. <Link to="/infra/new">Add resources</Link> from a VPC granted to your project, or
        ask an admin to grant one.
      </>
    ) : (
      'Nothing is watched yet in your projects.'
    )
  ) : (
    'No resources match this search and filter.'
  )
  return (
    <>
      <PageHeader
        title="Infrastructure"
        subtitle="AWS resources: EC2 servers in public and private subnets, Auto Scaling groups, and Application and Network Load Balancers."
      >
        {manager && (
          <Link to="/infra/settings" className="btn">
            AWS &amp; access
          </Link>
        )}
        {creator && (
          <Link to="/infra/new" className="btn btn-primary">
            Add resources
          </Link>
        )}
      </PageHeader>

      <ErrorBanner error={overview.error ?? everything.error ?? resources.error ?? summary.error} />

      <div className="stats-wrap">
        <div className="stats stats-infra">
          {FILTERS.map((f) => (
            <StateCard
              key={f.key}
              filter={f}
              counts={summary.data}
              active={filter === f.key}
              onPick={() => pick(setFilter)(f.key)}
            />
          ))}
        </div>
      </div>

      {ov && ov.vpcs.length > 0 && view !== 'map' && (
        <div className="vpc-cards">
          {ov.vpcs.map((vpc) => (
            <VpcCard key={vpc.id} vpc={vpc} />
          ))}
        </div>
      )}

      {ov && ov.by_kind.length > 0 && (
        <div className="kind-row">
          {ov.by_kind.map((k) => (
            <button
              type="button"
              key={k.kind}
              className={`kind-chip${kind === k.kind ? ' is-active' : ''}`}
              onClick={() => pick(setKind)(kind === k.kind ? '' : k.kind)}
            >
              <KindIcon kind={k.kind} />
              <span>
                <strong>{k.total}</strong>{' '}
                {k.total === 1 ? kindInfo(k.kind).noun : kindInfo(k.kind).nouns}
              </span>
              {k.down > 0 && <span className="text-down small">{k.down} down</span>}
              {k.degraded > 0 && <span className="text-pending small">{k.degraded} degraded</span>}
            </button>
          ))}
        </div>
      )}

      <div className="infra-bar">
        <div className="infra-bar-row">
          <label className="infra-search">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
              <circle cx="9" cy="9" r="5.5" />
              <path d="m13.5 13.5 4 4" />
            </svg>
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search by name, AWS id or address"
              aria-label="Search resources"
              maxLength={200}
            />
          </label>
          <div className="view-toggle" role="group" aria-label="View">
            {VIEWS.map(([value, label]) => (
              <button
                key={value}
                type="button"
                className={`btn btn-sm${view === value ? ' is-active' : ''}`}
                aria-pressed={view === value}
                onClick={() => setView(value)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
        <div className="infra-bar-row">
          <FilterPill
            label="Kind"
            value={kind}
            onChange={pick(setKind)}
            allLabel="All"
            options={KINDS.map((k) => ({ value: k.value, label: k.plural }))}
          />
          <FilterPill
            label="Environment"
            value={environment}
            onChange={pick(setEnvironment)}
            allLabel="All"
            options={ENVIRONMENTS.map((env) => ({ value: env.value, label: env.label }))}
          />
          {ov && ov.vpcs.length > 1 && (
            <FilterPill
              label="VPC"
              value={vpcId}
              onChange={pick(setVpcId)}
              allLabel="All"
              options={ov.vpcs.map((v) => ({ value: v.id, label: v.name }))}
            />
          )}
          {(kind || environment || vpcId) && (
            <button
              type="button"
              className="filter-clear"
              onClick={() => {
                setKind('')
                setEnvironment('')
                setVpcId('')
                setPage(0)
              }}
            >
              Clear filters
            </button>
          )}
        </div>
      </div>

      {view === 'map' ? (
        !ov || !everything.data ? (
          <Loading />
        ) : ov.vpcs.length ? (
          <InfraMap
            vpcs={ov.vpcs}
            resources={everything.data}
            topologies={topologies.data ?? undefined}
            match={filtered ? matches : null}
            settingsTo={manager ? '/infra/settings' : undefined}
          />
        ) : (
          <Empty>{emptyLabel}</Empty>
        )
      ) : resources.loading ? (
        <Loading />
      ) : (
        <ResourceTable resources={resources.data?.items ?? []} emptyLabel={emptyLabel} />
      )}

      {total > PAGE_SIZE && (
        <div className="pager">
          <span className="muted small">
            {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, total)} of {total}
          </span>
          <button type="button" className="btn btn-sm" onClick={() => setPage(page - 1)} disabled={page === 0}>
            Previous
          </button>
          <button type="button" className="btn btn-sm" onClick={() => setPage(page + 1)} disabled={page >= lastPage}>
            Next
          </button>
        </div>
      )}
    </>
  )
}
