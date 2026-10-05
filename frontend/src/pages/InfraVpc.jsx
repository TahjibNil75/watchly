import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { ConfirmDialog, Empty, ErrorBanner, Loading } from '../components.jsx'
import { dateTime, since, timeAgo } from '../format.js'
import { ResourceTable } from '../Infra.jsx'
import { STATES } from '../infra.js'
import { VpcMap } from '../NetworkMap.jsx'
import { canCreateProjects, canManageProject, canViewAllProjects } from '../roles.js'
import { useApi } from '../useApi.js'

const VIEW_KEY = 'watchly.vpcView'

function savedView() {
  try {
    return localStorage.getItem(VIEW_KEY) === 'list' ? 'list' : 'map'
  } catch {
    return 'map'
  }
}

function TestResult({ test }) {
  if (!test) return <p className="muted small">Not tested yet.</p>
  return (
    <ol className="steps">
      {test.steps.map((s) => (
        <li key={s.step} className={s.ok ? 'is-ok' : 'is-failed'}>
          <span className="step-mark" aria-hidden="true">
            {s.ok ? '✓' : '✗'}
          </span>
          <span className="step-name">{s.step}</span>
          <span className="step-time muted">{s.time_ms != null ? `${s.time_ms} ms` : ''}</span>
          <span className="step-detail">{s.detail}</span>
        </li>
      ))}
    </ol>
  )
}

export default function InfraVpc() {
  const { id } = useParams()
  const { user } = useAuth()
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [view, setView] = useState(savedView)
  const [refreshing, setRefreshing] = useState(false)

  const vpc = useApi(() => api.getVpc(id), [id], { pollMs: 30000 })
  const resources = useApi(() => api.listResources({ vpc_id: id, sort: 'name', limit: 200 }), [id], { pollMs: 30000 })
  const topology = useApi(() => (view === 'map' ? api.vpcTopology(id) : null), [id, view], { pollMs: 30000 })

  if (vpc.loading) return <Loading />
  if (!vpc.data) {
    return (
      <>
        <ErrorBanner error={vpc.error} />
        <Link to="/infra">← Back to infrastructure</Link>
      </>
    )
  }
  const v = vpc.data
  // Admin, DevOps, or the project manager who owns the VPC's project.
  const manager = canManageProject(user, v.project)

  async function run(action) {
    setBusy(true)
    setError(null)
    try {
      await action()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  const test = () =>
    run(async () => {
      await api.testVpc(v.id)
      vpc.reload()
    })
  const pickView = (next) => {
    setView(next)
    try {
      localStorage.setItem(VIEW_KEY, next)
    } catch {
      // Remembering the view is a nicety.
    }
  }
  const readAgain = async () => {
    setRefreshing(true)
    try {
      topology.setData(await api.vpcTopology(v.id, true))
    } catch (err) {
      setError(err)
    } finally {
      setRefreshing(false)
    }
  }
  const remove = () =>
    run(async () => {
      await api.deleteVpc(v.id, { withResources: held > 0 })
      navigate(`/projects/${v.project.id}`)
    })

  // Monitored resources in it, which keep it from being deleted.
  const held = resources.data?.total ?? 0

  // Resources grouped by the subnet AWS put them in.
  const bySubnet = {}
  for (const r of resources.data?.items ?? []) {
    const subnet =
      r.aws_detail?.subnet ??
      { load_balancer: 'Load balancers (span subnets)', auto_scaling_group: 'Auto Scaling groups (span subnets)' }[r.kind] ??
      'Subnet unknown'
    ;(bySubnet[subnet] ??= []).push(r)
  }

  return (
    <>
      <p className="crumbs">
        <Link to="/infra">Infrastructure</Link>
        {' / '}
        <Link to={`/projects/${v.project.id}`}>{v.project.name}</Link>
        {canViewAllProjects(user) && (
          <>
            {' / '}
            <Link to="/infra/settings">AWS &amp; access</Link>
          </>
        )}
      </p>
      <header className="page-header">
        <div>
          <h1>
            {v.name}{' '}
            <span className={`badge badge-${v.health === 'unreachable' ? 'down' : v.health === 'untested' ? 'unknown' : 'healthy'}`}>
              {v.health}
            </span>
          </h1>
          <p className="muted">
            <code>{v.aws_vpc_id}</code> · {v.account.name}
            {v.account.aws_account_id ? ` (${v.account.aws_account_id})` : ''} · {v.region} · {v.cidrs.join(', ')}
            {v.is_watchly_vpc && ' · Watchly runs here'}
          </p>
          {v.description && <p className="muted small">{v.description}</p>}
        </div>
        {manager && (
          <div className="actions">
            <button type="button" className="btn btn-primary" onClick={test} disabled={busy}>
              {busy ? 'Testing…' : 'Test now'}
            </button>
          </div>
        )}
      </header>

      <ErrorBanner error={error ?? vpc.error} />
      {v.unreachable_since && (
        <div className="banner banner-error">
          Unreachable for {since(v.unreachable_since)}: most of its checks fail to connect. A security group, NACL,
          route or peering change is the usual cause; each resource&apos;s own down alert waits until the VPC answers.
        </div>
      )}

      <div className="stats-wrap">
        <div className="kv-inline">
          <div>
            <span>Resources</span>
            <strong>{v.counts.total}</strong>
          </div>
          {STATES.filter((s) => v.counts[s.key]).map((s) => (
            <div key={s.key}>
              <span>{s.label}</span>
              <strong>{v.counts[s.key]}</strong>
            </div>
          ))}
        </div>
      </div>

      <div className="grid-2">
        <section className="card">
          <h2>Last test</h2>
          {v.last_tested_at && <p className="muted small">{dateTime(v.last_tested_at)}</p>}
          <TestResult test={v.last_test} />
          <p className="muted small">
            Placement, AWS access, and a TCP connection (no login) to up to five resources, one per subnet.
          </p>
        </section>
        <section className="card">
          <h2>Project and account</h2>
          <dl className="kv">
            <div>
              <dt>Project</dt>
              <dd>
                <Link to={`/projects/${v.project.id}`}>{v.project.name}</Link>
              </dd>
            </div>
            <div>
              <dt>AWS account</dt>
              <dd>
                {v.account.name}
                {v.account.aws_account_id && <span className="muted record"> · {v.account.aws_account_id}</span>}
              </dd>
            </div>
          </dl>
          <p className="muted small">
            Watchly reads this VPC with the account&apos;s credentials, and only the project&apos;s resources live in it.
          </p>
          {v.synced_at && <p className="muted small">Resources last read from AWS {timeAgo(v.synced_at)}.</p>}
        </section>
      </div>

      <div className="section-head-row section-spaced">
        <h2>{view === 'map' ? 'Network map' : 'Resources by subnet'}</h2>
        <div className="view-toggle" role="group" aria-label="View">
          {[
            ['map', 'Map'],
            ['list', 'List'],
          ].map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={`btn btn-sm${view === value ? ' is-active' : ''}`}
              aria-pressed={view === value}
              onClick={() => pickView(value)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      {view === 'map' ? (
        topology.loading ? (
          <Loading label="Reading the VPC from AWS…" />
        ) : topology.data ? (
          <VpcMap
            topology={topology.data}
            vpc={v}
            resources={resources.data?.items}
            canAdd={canCreateProjects(user) && manager}
            onRefresh={manager ? readAgain : undefined}
            refreshing={refreshing}
          />
        ) : (
          <ErrorBanner error={topology.error} />
        )
      ) : resources.loading ? (
        <Loading />
      ) : !resources.data?.items.length ? (
        <Empty>
          Nothing watched in {v.name} yet. <Link to={`/infra/new?project=${v.project.id}`}>Add resources</Link>.
        </Empty>
      ) : (
        Object.entries(bySubnet)
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([subnet, items]) => (
            <section key={subnet} className="section">
              <h3>{subnet}</h3>
              <ResourceTable resources={items} showVpc={false} emptyLabel="" />
            </section>
          ))
      )}

      {manager && (
        <section className="card card-danger section-spaced">
          <h2>Delete VPC</h2>
          <p className="muted small">
            {held > 0
              ? `Stops monitoring the ${held} ${held === 1 ? 'resource' : 'resources'} in it, deleting their checks and history, then forgets the VPC. To remove one resource only, open it and use Stop monitoring at the bottom of its page.`
              : 'Watchly forgets this VPC.'}{' '}
            Nothing changes in AWS.
          </p>
          <button type="button" className="btn btn-danger-solid" onClick={() => setConfirmingDelete(true)} disabled={busy}>
            {held > 0 ? `Delete VPC and ${held} ${held === 1 ? 'resource' : 'resources'}` : 'Delete VPC'}
          </button>
        </section>
      )}
      {confirmingDelete && (
        <ConfirmDialog
          title={held > 0 ? `Delete ${v.name} and its ${held} ${held === 1 ? 'resource' : 'resources'}?` : `Delete ${v.name}?`}
          busy={busy}
          error={error}
          onConfirm={remove}
          onCancel={() => setConfirmingDelete(false)}
        >
          {held > 0 && (
            <>
              Watchly stops monitoring{' '}
              {(resources.data?.items ?? []).map((r) => r.name).join(', ')}
              {resources.data && held > resources.data.items.length ? ` and ${held - resources.data.items.length} more` : ''}, and
              deletes their checks, history and events.{' '}
            </>
          )}
          Watchly stops knowing this VPC. Nothing changes in AWS.
        </ConfirmDialog>
      )}
    </>
  )
}
