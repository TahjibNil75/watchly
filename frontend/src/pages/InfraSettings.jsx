import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { Mark } from '../AwsAccounts.jsx'
import { Empty, ErrorBanner, Loading, PageHeader } from '../components.jsx'
import { useInfraEnabled } from '../infra.js'
import { canViewAllProjects } from '../roles.js'
import { useApi } from '../useApi.js'

function WhereWatchlyRuns() {
  const self = useApi(() => api.infraSelf(), [])
  if (self.loading) return <Loading />
  if (!self.data) return <ErrorBanner error={self.error} />
  const s = self.data
  const i = s.instance
  return (
    <div className="grid-2">
      <section className="card">
        <h2>Where Watchly runs</h2>
        {i ? (
          <dl className="kv">
            <div>
              <dt>Instance</dt>
              <dd className="record">
                {i.id} · {i.type} · {i.az}
              </dd>
            </div>
            <div>
              <dt>Network</dt>
              <dd className="record">
                {i.vpc_id} · {i.subnet_id} · {i.private_ip}
                {i.public_ip ? ` · public ${i.public_ip}` : ''}
              </dd>
            </div>
            <div>
              <dt>Security groups</dt>
              <dd>{i.security_groups.map((g) => `${g.name || g.id} (${g.id})`).join(', ') || '—'}</dd>
            </div>
            <div>
              <dt>Instance role</dt>
              <dd>{i.iam_role ?? <span className="text-down">none: AWS calls will fail</span>}</dd>
            </div>
            {s.imds && (
              <div>
                <dt>Metadata service</dt>
                <dd>
                  {s.imds.tokens_required ? (
                    'IMDSv2 required'
                  ) : (
                    <span className="text-down">IMDSv1 still allowed: require tokens (HttpTokens=required)</span>
                  )}
                  {s.imds.hop_limit != null && ` · hop limit ${s.imds.hop_limit}`}
                </dd>
              </div>
            )}
          </dl>
        ) : (
          <p className="muted">
            Not on EC2: no instance metadata answered. Infrastructure checks still run where this server&apos;s network
            reaches, with the AWS credentials it has.
          </p>
        )}
        <dl className="kv">
          <div>
            <dt>Region</dt>
            <dd>{s.region ?? <span className="text-down">none: set AWS_REGION</span>}</dd>
          </div>
          <div>
            <dt>AWS identity</dt>
            <dd className="record">{s.caller ? `${s.caller.arn}` : <span className="text-down">{s.caller_error}</span>}</dd>
          </div>
        </dl>
      </section>
      <section className="card">
        <h2>Egress guard</h2>
        <dl className="kv">
          <div>
            <dt>Instance metadata</dt>
            <dd>{s.guard.metadata_blocked ? 'Refused to every check' : <span className="text-down">reachable</span>}</dd>
          </div>
          <div>
            <dt>Website checks</dt>
            <dd>
              {s.guard.website_private_targets === 'block' ? (
                'Public addresses only'
              ) : (
                <span className="text-down">May reach private addresses (WEBSITE_PRIVATE_TARGETS=allow)</span>
              )}
            </dd>
          </div>
          <div>
            <dt>Infrastructure checks</dt>
            <dd>
              The ranges of their resource&apos;s VPC; public addresses only for an internet-facing load balancer or a
              server&apos;s public IP
            </dd>
          </div>
          <div>
            <dt>This server&apos;s addresses</dt>
            <dd className="record">{s.guard.own_addresses.join(', ') || '—'}</dd>
          </div>
          {s.guard.deny_extra.length > 0 && (
            <div>
              <dt>Also refused</dt>
              <dd className="record">{s.guard.deny_extra.join(', ')}</dd>
            </div>
          )}
        </dl>
        <h3>What this server can probe</h3>
        <p>
          {Object.entries(s.probes).map(([name, ok]) => (
            <span key={name} className="chip">
              <Mark ok={ok} /> {name}
            </span>
          ))}
        </p>
      </section>
      <section className="card">
        <h2>What Watchly&apos;s own role may do</h2>
        <p className="muted small">Each project&apos;s AWS accounts are tested with their own credentials, on the project&apos;s page.</p>
        <ul className="perm-list">
          {s.permissions.map((p) => (
            <li key={p.action}>
              <Mark ok={p.ok} />
              <code>{p.action}</code>
              {p.detail && <span className="muted small"> · {p.detail}</span>}
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}

// Every VPC a project registered, by adding a resource from one of its AWS
// accounts. Read-only here: each VPC's page tests, renames or removes it.
function Vpcs() {
  const vpcs = useApi(() => api.listVpcs(), [])
  return (
    <section className="card">
      <h2>VPCs</h2>
      <p className="muted small">
        Infrastructure checks may reach a VPC&apos;s address ranges, read from AWS, and the public addresses AWS gives
        its internet-facing load balancers and public servers. A VPC is registered when a project first adds a
        resource from it, through one of the project&apos;s AWS accounts.
      </p>
      <ErrorBanner error={vpcs.error} />
      {vpcs.data?.items.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>VPC</th>
                <th>Project</th>
                <th>Account</th>
                <th>Ranges</th>
                <th>Resources</th>
                <th>Health</th>
              </tr>
            </thead>
            <tbody>
              {vpcs.data.items.map((v) => (
                <tr key={v.id}>
                  <td>
                    <Link to={`/infra/vpcs/${v.id}`} className="strong-link">
                      {v.name}
                    </Link>
                    <div className="muted small record">
                      {v.aws_vpc_id} · {v.region}
                    </div>
                  </td>
                  <td>
                    <Link to={`/projects/${v.project.id}`}>{v.project.name}</Link>
                  </td>
                  <td>
                    {v.account.name}
                    {v.account.aws_account_id && <div className="muted small record">{v.account.aws_account_id}</div>}
                  </td>
                  <td className="record">{v.cidrs.join(', ')}</td>
                  <td className="num">{v.counts.total}</td>
                  <td>
                    <span className={`badge badge-${v.health === 'unreachable' ? 'down' : v.health === 'untested' ? 'unknown' : 'healthy'}`}>
                      {v.health}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        !vpcs.loading && <Empty>No VPC yet: add resources to an infrastructure project to register its VPCs.</Empty>
      )}
    </section>
  )
}

function BlockedWebsites() {
  const blocked = useApi(() => api.blockedWebsites(), [])
  return (
    <section className="card">
      <h2>Website checks the egress guard refuses</h2>
      <p className="muted small">
        Website checks reach public addresses only, so a server inside a VPC cannot be used to probe it. These checks
        point somewhere they may not go; watch private targets from Infrastructure instead.
      </p>
      <ErrorBanner error={blocked.error} />
      {blocked.loading ? (
        <Loading />
      ) : !blocked.data?.blocked.length ? (
        <p className="muted">None of {blocked.data?.checked ?? 0} HTTP and ping checks is refused.</p>
      ) : (
        <ul className="perm-list">
          {blocked.data.blocked.map((b) => (
            <li key={b.website_id}>
              <Mark ok={false} />
              <Link to={`/websites/${b.website_id}`}>{b.name}</Link>
              <span className="muted small"> · {b.reason}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

export default function InfraSettings() {
  const { user } = useAuth()
  const enabled = useInfraEnabled()
  if (!canViewAllProjects(user)) return <Empty>Only admins and DevOps can manage AWS access.</Empty>
  return (
    <>
      <p className="crumbs">
        <Link to="/infra">Infrastructure</Link>
      </p>
      <PageHeader title="AWS & access" subtitle="Where Watchly runs, what it may reach, and the VPCs infrastructure checks use." />
      {enabled === null ? (
        <Loading />
      ) : enabled ? (
        <>
          <WhereWatchlyRuns />
          <Vpcs />
        </>
      ) : (
        <Empty>
          Infrastructure monitoring is off. Set <code>INFRA_AWS_ENABLED=true</code> on the API to watch AWS.
        </Empty>
      )}
      <BlockedWebsites />
    </>
  )
}
