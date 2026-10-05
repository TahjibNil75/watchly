// An infrastructure project's CodeDeploy deployments, as Watchly saw them in
// the AWS accounts that watch them. While one runs, the resources it deploys
// to are in maintenance. Mirrors /monitoring/infra/aws/deployments.
import { Link } from 'react-router-dom'
import { api } from './api.js'
import { EnvironmentBadge, ErrorBanner } from './components.jsx'
import { dateTime, duration, timeAgo } from './format.js'
import { useApi } from './useApi.js'

// CodeDeploy's states, as a badge.
const STATUS = {
  Succeeded: ['badge-healthy', 'succeeded'],
  Failed: ['badge-down', 'failed'],
  Stopped: ['badge-degraded', 'stopped'],
  Unwatched: ['badge-unknown', 'no longer watched'],
}

function StatusBadge({ deployment: d }) {
  if (d.is_active) return <span className="badge badge-maintenance">{d.status === 'Ready' ? 'awaiting reroute' : 'deploying'}</span>
  const [cls, label] = STATUS[d.status] ?? ['badge-unknown', d.status.toLowerCase()]
  return (
    <span className={`badge ${cls}`} title={d.error ?? undefined}>
      {label}
    </span>
  )
}

const took = (d) => duration((new Date(d.finished_at ?? Date.now()) - new Date(d.started_at)) / 1000)

export default function DeploymentsSection({ projectId, canManage }) {
  const deployments = useApi(() => api.listDeployments({ project_id: projectId, limit: 10 }), [projectId], {
    pollMs: 30000,
  })
  const items = deployments.data?.items ?? []
  // Nothing to show to someone who cannot turn it on.
  if (!items.length && !canManage && !deployments.error) return null

  return (
    <section className="section">
      <h2>Deployments</h2>
      <ErrorBanner error={deployments.error} />
      {deployments.loading ? null : items.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Deployment</th>
                <th>Environment</th>
                <th>Status</th>
                <th>Started</th>
                <th>Took</th>
                <th>Alerts paused for</th>
              </tr>
            </thead>
            <tbody>
              {items.map((d) => (
                <tr key={d.id}>
                  <td>
                    <strong>{d.application_name}</strong> <span className="muted">/ {d.group_name}</span>
                    <div className="muted small record nowrap">
                      {d.deployment_id} · {d.account.name} · {d.region}
                    </div>
                    {d.revision && <div className="muted small record">{d.revision}</div>}
                    {d.description && <div className="muted small">{d.description}</div>}
                  </td>
                  <td>
                    <EnvironmentBadge environment={d.environment} />
                    {!d.environment && <span className="muted">—</span>}
                  </td>
                  <td>
                    <StatusBadge deployment={d} />
                    {d.error && <div className="muted small">{d.error}</div>}
                  </td>
                  <td className="nowrap" title={dateTime(d.started_at)}>
                    {timeAgo(d.started_at)}
                  </td>
                  <td className="nowrap">{took(d)}</td>
                  <td className="small">
                    {d.fallback ? (
                      <span title={d.fallback}>every server, load balancer and group in {d.region}</span>
                    ) : d.resources.length ? (
                      d.resources.map((r, i) => (
                        <span key={r.id}>
                          {i > 0 && ', '}
                          <Link to={`/infra/resources/${r.id}`}>{r.name}</Link>
                        </span>
                      ))
                    ) : (
                      <span className="muted">none monitored</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted small">
          No deployments seen yet. Turn on &ldquo;Watch CodeDeploy deployments&rdquo; on an AWS account below: each
          deployment is then announced, and its resources send no down alerts until it ends.
        </p>
      )}
    </section>
  )
}
