// The Infrastructure page's service cards: one card per service, drawn as the
// chain traffic follows, load balancer then Auto Scaling group then its
// instances, then the databases they use, with what is wrong underneath.
// Grouping is in infra.js.
import { Link } from 'react-router-dom'
import { EnvironmentBadge, Empty } from './components.jsx'
import { since } from './format.js'
import { InfraIcon, KindIcon } from './Infra.jsx'
import { exposureLine, groupInstances, groupSummary, resourceTrouble, stateLabel } from './infra.js'

const ALERT = new Set(['down', 'degraded', 'missing'])

function StateChip({ state, to, title, children }) {
  const classes = `svc-chip svc-st-${state}`
  return to ? (
    <Link to={to} className={classes} title={title}>
      {children}
    </Link>
  ) : (
    <span className={classes} title={title}>
      {children}
    </span>
  )
}

function Part({ resource }) {
  return (
    <div className={`svc-part svc-st-${resource.state}`}>
      <span className="svc-dot" title={stateLabel(resource.state)} />
      <span className="svc-part-text">
        <Link to={`/infra/resources/${resource.id}`} className="strong-link">
          {resource.name}
        </Link>
        <span className="muted small">{exposureLine(resource)}</span>
      </span>
    </div>
  )
}

function Stage({ kind, label, grow, children }) {
  return (
    <div className={`svc-stage${grow ? ' is-grow' : ''}`}>
      <span className="svc-cap">
        <KindIcon kind={kind} />
        {label}
      </span>
      {children}
    </div>
  )
}

const Arrow = () => (
  <InfraIcon className="svc-arrow">
    <path d="M5 12h14M13 6l6 6-6 6" />
  </InfraIcon>
)

// What is wrong, one line per resource or instance.
function issues(service) {
  const lines = []
  for (const r of service.resources) {
    if (ALERT.has(r.state)) {
      const started = r.down_since ?? r.degraded_since ?? r.missing_since
      lines.push({ key: r.id, state: r.state, name: r.name, text: resourceTrouble(r) ?? stateLabel(r.state), since: started })
    }
  }
  return lines
}

// What an ungrouped card holds: servers, databases, or both.
const ungroupedTitle = (servers, databases) =>
  `Ungrouped ${[servers.length && 'servers', databases.length && 'databases'].filter(Boolean).join(' and ') || 'servers'}`

function ServiceCard({ service, showVpc }) {
  const lbs = service.resources.filter((r) => r.kind === 'load_balancer')
  const groups = service.resources.filter((r) => r.kind === 'auto_scaling_group')
  const servers = service.resources.filter((r) => r.kind === 'server')
  const databases = service.resources.filter((r) => r.kind === 'database')
  const problems = issues(service)
  const projects = [...new Set(service.resources.map((r) => r.project.name))]
  const alert = ALERT.has(service.state)
  const environments = [...new Set(service.resources.map((r) => r.environment).filter(Boolean))]

  return (
    <article className={`card service-card svc-st-${service.state}${alert ? ' is-alert' : ''}`}>
      <header className="service-head">
        <div className="service-title">
          <strong>{service.name ?? ungroupedTitle(servers, databases)}</strong>
          {showVpc && <span className="muted small">{service.vpc.name}</span>}
          {environments.map((env) => (
            <EnvironmentBadge key={env} environment={env} />
          ))}
        </div>
        <span className={`badge badge-${service.state}`}>{groupSummary(service.counts)}</span>
      </header>

      {(lbs.length > 0 || groups.length > 0) && (
        <div className="svc-flow">
          {lbs.length > 0 && (
            <Stage kind="load_balancer" label={lbs.length === 1 ? 'Load balancer' : 'Load balancers'}>
              {lbs.map((r) => (
                <Part key={r.id} resource={r} />
              ))}
            </Stage>
          )}
          {lbs.length > 0 && groups.length > 0 && <Arrow />}
          {groups.length > 0 && (
            <Stage kind="auto_scaling_group" label={groups.length === 1 ? 'Auto Scaling group' : 'Auto Scaling groups'} grow>
              {groups.map((r) => {
                const instances = groupInstances(r)
                return (
                  <div key={r.id} className="svc-group">
                    <Part resource={r} />
                    {instances.length > 0 && (
                      <div className="svc-chips">
                        {instances.map((i) => (
                          <StateChip key={i.id} state={i.state} title={i.note ? `${i.id}: ${i.note}` : i.id}>
                            {i.id}
                          </StateChip>
                        ))}
                      </div>
                    )}
                  </div>
                )
              })}
            </Stage>
          )}
        </div>
      )}

      {servers.length > 0 && (
        <Stage kind="server" label={servers.length === 1 ? 'Server' : 'Servers'}>
          <div className="svc-chips">
            {servers.map((r) => (
              <StateChip key={r.id} state={r.state} to={`/infra/resources/${r.id}`} title={`${stateLabel(r.state)} · ${exposureLine(r)}`}>
                {r.name}
              </StateChip>
            ))}
          </div>
        </Stage>
      )}

      {databases.length > 0 && (
        <Stage kind="database" label={databases.length === 1 ? 'Database' : 'Databases'}>
          <div className="svc-chips">
            {databases.map((r) => (
              <StateChip key={r.id} state={r.state} to={`/infra/resources/${r.id}`} title={`${stateLabel(r.state)} · ${exposureLine(r)}`}>
                {r.name}
              </StateChip>
            ))}
          </div>
        </Stage>
      )}

      {problems.length > 0 ? (
        <ul className="svc-issues">
          {problems.map((p) => (
            <li key={p.key} className={`svc-st-${p.state}`}>
              <span className="svc-dot" />
              <span className="svc-issue-text">
                <strong>{p.name}</strong> {p.text}
              </span>
              {p.since && <span className="muted small nowrap">for {since(p.since)}</span>}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted small svc-ok">Nothing needs attention.</p>
      )}

      <footer className="service-foot muted small">
        <span>
          {service.counts.total} {service.counts.total === 1 ? 'resource' : 'resources'}
        </span>
        <span className="truncate">{projects.join(', ')}</span>
      </footer>
    </article>
  )
}

export default function ServiceCards({ services, showVpc, emptyLabel }) {
  if (!services.length) return <Empty>{emptyLabel}</Empty>
  return (
    <>
      <p className="muted small service-note">
        A service is a load balancer and Auto Scaling group that share a target group, plus the servers behind them.
        Tag resources <code>Service</code> in AWS to name one or to join parts the load balancer does not link, such as
        the database it uses.
      </p>
      <div className="service-grid">
        {services.map((s) => (
          <ServiceCard key={s.id} service={s} showVpc={showVpc} />
        ))}
      </div>
    </>
  )
}
