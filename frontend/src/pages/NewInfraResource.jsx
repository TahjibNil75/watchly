import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { Empty, ErrorBanner, Loading, PageHeader } from '../components.jsx'
import { ENVIRONMENTS } from '../environments.js'
import { CheckTypeBadge, DiagnoseSteps, KindBadge, KindIcon, ProviderPicker, RegionSelect } from '../Infra.jsx'
import { CHECK_TYPES, PUBLIC_IP_CHECKS, useInfraEnabled } from '../infra.js'
import { canCreateProjects, canManageProject } from '../roles.js'
import { timeAgo } from '../format.js'
import { useApi } from '../useApi.js'

const settingsText = (settings) =>
  Object.entries(settings)
    .map(([k, v]) => `${k.replace(/_/g, ' ')} ${String(v).split('/').slice(-2).join('/')}`)
    .join(' · ')

// A suggested check's settings: its target group's health check is only where
// it starts, so what the user typed over it wins, and it goes to the server's
// public IP when its choice says so.
function settingsFor(suggestion, choice, index) {
  const settings = { ...suggestion.settings, ...(choice.edits?.[index] ?? {}) }
  if (choice.publicIp && PUBLIC_IP_CHECKS.has(suggestion.check_type)) settings.use_public_ip = true
  return settings
}

// Blank text and number inputs mean "not set", so the API's defaults apply.
const cleaned = (settings) =>
  Object.fromEntries(Object.entries(settings).filter(([, v]) => v !== '' && v !== undefined))

// The check's name follows an edited path: "GET /health on each instance".
function nameFor(suggestion, choice, index) {
  const path = choice.edits?.[index]?.path
  if (!path || !suggestion.settings.path) return suggestion.name
  return suggestion.name.replace(suggestion.settings.path, path)
}

// One discovered resource: pick it, then pick which of its suggested checks to
// add, and try each before saving. A server with a public IP, or a group with
// public instances, may be checked there instead of at the private IPs.
function Item({ item, chosen, onToggle, choice, onChoice, diagnoseBase }) {
  const monitored = item.monitored_by.length > 0
  const [results, setResults] = useState({})
  const [testing, setTesting] = useState(null)

  const test = async (index, suggestion) => {
    setTesting(index)
    try {
      const result = await api.infraDiagnose({
        ...diagnoseBase,
        kind: item.kind,
        aws_id: item.aws_id,
        check: { check_type: suggestion.check_type, settings: cleaned(settingsFor(suggestion, choice, index)) },
      })
      setResults({ ...results, [index]: result })
    } catch (err) {
      setResults({ ...results, [index]: { ok: false, failed_step: 'request', summary: err.message, steps: [] } })
    } finally {
      setTesting(null)
    }
  }

  // What the user typed over a suggestion; a test run before is stale.
  const edit = (index, change) => {
    onChoice({ ...choice, edits: { ...choice.edits, [index]: { ...choice.edits?.[index], ...change } } })
    setResults({})
  }

  return (
    <li className={`discovered${chosen ? ' is-chosen' : ''}${monitored ? ' is-monitored' : ''}`}>
      <label className="discovered-head">
        <input type="checkbox" checked={chosen} disabled={monitored} onChange={onToggle} />
        <span className="attention-icon">
          <KindIcon kind={item.kind} />
        </span>
        <span className="discovered-title">
          <strong>{item.name}</strong> <KindBadge kind={item.kind} />{' '}
          <span className="chip">{item.public ? 'public' : 'private'}</span>
          <span className="muted small">
            {' '}
            {item.detail}
            {item.subnet && ` · ${item.subnet}`}
          </span>
          <span className="muted small record">{item.address ?? item.aws_id}</span>
          {item.auto_scaling_group && (
            <span className="small text-pending">
              In Auto Scaling group {item.auto_scaling_group}, which replaces it when it scales: watch the group
              instead, listed here too, and its checks run on every instance it has.
            </span>
          )}
          {item.kind === 'auto_scaling_group' && item.instances.length > 0 && (
            <span className="small">
              {item.instances.map((i) => (
                <span key={i.id} className={`chip${i.health_status !== 'Healthy' ? ' text-down' : ''}`}>
                  {i.id} · {i.lifecycle_state}
                  {i.health_status !== 'Healthy' ? ` · ${i.health_status}` : ''}
                </span>
              ))}
            </span>
          )}
          {monitored && (
            <span className="small muted">
              Already monitored
              {item.monitored_by[0].project ? ` by ${item.monitored_by[0].project.name}` : ''} ·{' '}
              <Link to={`/infra/resources/${item.monitored_by[0].resource_id}`}>open</Link>
            </span>
          )}
        </span>
      </label>
      {chosen && ((item.kind === 'server' && item.public_ip) || (item.kind === 'auto_scaling_group' && item.public)) && (
        <label className="suggestion">
          <input
            type="checkbox"
            checked={Boolean(choice.publicIp)}
            onChange={(e) => {
              onChoice({ ...choice, publicIp: e.target.checked })
              setResults({})
            }}
          />
          <span>
            {item.kind === 'auto_scaling_group' ? (
              "Check each instance's public IP, over the internet"
            ) : (
              <>
                Check its public IP, <code>{item.public_ip}</code>, over the internet
              </>
            )}
            <span className="muted small"> · instead of the private IP, from inside the VPC</span>
          </span>
        </label>
      )}
      {chosen && (
        <ul className="suggestions">
          {item.suggested.map((s, index) => (
            <li key={`${s.check_type}-${index}`}>
              <label className="suggestion">
                <input
                  type="checkbox"
                  checked={choice.checks[index]}
                  onChange={(e) => onChoice({ ...choice, checks: { ...choice.checks, [index]: e.target.checked } })}
                />
                <CheckTypeBadge type={s.check_type} />
                <span>
                  {nameFor(s, choice, index)}
                  <span className="muted small"> · {CHECK_TYPES[s.check_type]?.note}</span>
                  {Object.keys(s.settings).length > 0 && <span className="muted small"> · {settingsText(settingsFor(s, choice, index))}</span>}
                </span>
              </label>
              {s.check_type === 'http' && choice.checks[index] && (
                <div className="suggestion-edit">
                  <label className="field">
                    <span>Path</span>
                    <input
                      value={settingsFor(s, choice, index).path ?? ''}
                      maxLength={1024}
                      onChange={(e) => edit(index, { path: e.target.value })}
                      placeholder="/health"
                    />
                  </label>
                  <label className="field">
                    <span>Expected status</span>
                    <input
                      type="number"
                      min={100}
                      max={599}
                      value={settingsFor(s, choice, index).expected_status ?? 200}
                      onChange={(e) => edit(index, { expected_status: e.target.value === '' ? '' : Number(e.target.value) })}
                    />
                  </label>
                  <label className="field">
                    <span>Host header</span>
                    <input
                      value={settingsFor(s, choice, index).host_header ?? ''}
                      maxLength={255}
                      onChange={(e) => edit(index, { host_header: e.target.value })}
                      placeholder="for host-based rules"
                    />
                  </label>
                  <small className="muted">
                    Starts from the target group's health check; the server may answer elsewhere, so set what it really serves.
                  </small>
                </div>
              )}
              <button
                type="button"
                className="btn btn-sm btn-ghost"
                onClick={() => test(index, s)}
                disabled={testing !== null || !diagnoseBase.project_id}
                title={diagnoseBase.project_id ? undefined : 'Pick a project first'}
              >
                {testing === index ? 'Testing…' : 'Test connection'}
              </button>
              {results[index] && <DiagnoseSteps result={results[index]} />}
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

export default function NewInfraResource() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const enabled = useInfraEnabled()
  const allowed = canCreateProjects(user)
  // AWS is the only provider built; the picker shows the ones to come.
  const [provider, setProvider] = useState('aws')
  const [projectId, setProjectId] = useState(params.get('project') ?? '')
  const [accountId, setAccountId] = useState('')
  const [region, setRegion] = useState('')
  const [listedRegion, setListedRegion] = useState('')
  const [awsVpcId, setAwsVpcId] = useState('')
  const [vpcId, setVpcId] = useState('')
  const [environment, setEnvironment] = useState('')
  const [discovery, setDiscovery] = useState(null)
  const [chosen, setChosen] = useState({})
  const [choices, setChoices] = useState({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  // From the VPC map's "Monitor this": `vpc` is a registered VPC, `pick` the
  // discovery key of what to tick once it is listed.
  const preset = useRef({ vpc: params.get('vpc'), pick: params.get('pick') })
  useEffect(() => {
    const { vpc: presetVpc, pick } = preset.current
    if (!presetVpc || !allowed) return undefined
    let live = true
    ;(async () => {
      setBusy(true)
      try {
        const v = await api.getVpc(presetVpc)
        if (!live) return
        setProjectId(String(v.project.id))
        setAccountId(String(v.account.id))
        setRegion(v.region)
        setListedRegion(v.region)
        setAwsVpcId(v.aws_vpc_id)
        setVpcId(String(v.id))
        const result = await api.discoverVpc(v.id)
        if (!live) return
        setDiscovery(result)
        const item = result.items.find((i) => i.key === pick)
        if (item && !item.monitored_by.length) {
          setChosen({ [item.key]: true })
          setChoices({ [item.key]: { checks: Object.fromEntries(item.suggested.map((_, i) => [i, true])), publicIp: false } })
        }
      } catch (err) {
        if (live) setError(err)
      } finally {
        if (live) setBusy(false)
      }
    })()
    return () => {
      live = false
    }
  }, [allowed])

  const projects = useApi(
    () => (enabled && allowed ? api.listProjects({ monitors: 'infrastructure' }) : null),
    [enabled, allowed],
  )
  const accounts = useApi(() => (projectId ? api.listAwsAccounts(projectId) : null), [projectId])
  const accountItems = accounts.data?.items ?? []
  // One account needs no choosing.
  const account = accountItems.find((a) => String(a.id) === accountId) ?? (accountItems.length === 1 ? accountItems[0] : null)
  // The account's own environment is the starting point; picking one overrides it.
  const chosenEnvironment = environment || account?.environment || ''
  const available = useApi(
    () => (account ? api.availableVpcs(account.id, listedRegion || undefined) : null),
    [account?.id, listedRegion],
  )

  if (!allowed) return <Empty>Only admins, DevOps and project managers can add resources.</Empty>
  if (enabled === false) return <Empty>Infrastructure monitoring is off on this server.</Empty>
  if (enabled === null || projects.loading) return <Loading />

  const manageable = (projects.data?.items ?? []).filter((p) => canManageProject(user, p))
  const vpc = (available.data?.vpcs ?? []).find((v) => v.aws_vpc_id === awsVpcId)

  const reset = () => {
    setAwsVpcId('')
    setVpcId('')
    setDiscovery(null)
  }

  async function discover(refresh = false) {
    setBusy(true)
    setError(null)
    try {
      // Registered for this project the first time a resource is added from it.
      let id = vpcId || vpc.registered_as
      if (!id) {
        const created = await api.createVpc({ account_id: account.id, aws_vpc_id: vpc.aws_vpc_id, region: available.data.region })
        id = created.id
        available.reload()
      }
      setVpcId(String(id))
      const result = await api.discoverVpc(id, refresh)
      setDiscovery(result)
      setChosen({})
      setChoices({})
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  const toggle = (item) => {
    const next = !chosen[item.key]
    setChosen({ ...chosen, [item.key]: next })
    if (next && !choices[item.key]) {
      // Everything suggested is ticked.
      const checks = Object.fromEntries(item.suggested.map((s, i) => [i, true]))
      setChoices({ ...choices, [item.key]: { checks, publicIp: false } })
    }
  }

  const picked = (discovery?.items ?? []).filter((item) => chosen[item.key])

  async function save() {
    setBusy(true)
    setError(null)
    try {
      const items = picked.map((item) => {
        const choice = choices[item.key]
        const checks = item.suggested
          .map((s, i) => ({ s, i }))
          .filter(({ i }) => choice.checks[i])
          .map(({ s, i }) => ({
            check_type: s.check_type,
            name: choice.publicIp && PUBLIC_IP_CHECKS.has(s.check_type) ? `${nameFor(s, choice, i)} (public IP)` : nameFor(s, choice, i),
            settings: cleaned(settingsFor(s, choice, i)),
          }))
        return { key: item.key, checks }
      })
      if (!chosenEnvironment) throw new Error('Select an environment.')
      const empty = items.find((item) => !item.checks.length)
      if (empty) throw new Error(`Pick at least one check for ${picked.find((p) => p.key === empty.key).name}.`)
      const result = await api.importResources(vpcId, { project_id: Number(projectId), environment: chosenEnvironment, items })
      navigate(result.created.length === 1 ? `/infra/resources/${result.created[0].id}` : `/projects/${projectId}`)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        title="Add resources"
        subtitle="Pick a project, one of its AWS accounts and a VPC: Watchly reads what is in it and suggests checks."
      />
      <ErrorBanner error={error ?? projects.error ?? accounts.error ?? available.error} />

      {!manageable.length ? (
        <Empty>
          You manage no infrastructure project yet. <Link to="/projects">Create one</Link>, choosing
          &ldquo;Infrastructure&rdquo; and adding its AWS account.
        </Empty>
      ) : (
        <section className="card form">
          <ProviderPicker value={provider} onChange={setProvider} />
          <div className="row-3">
            <label className="field">
              <span>Project</span>
              <select
                value={projectId}
                onChange={(e) => {
                  setProjectId(e.target.value)
                  setAccountId('')
                  reset()
                }}
              >
                <option value="">Pick a project…</option>
                {manageable.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>AWS account</span>
              <select
                value={account ? String(account.id) : ''}
                onChange={(e) => {
                  setAccountId(e.target.value)
                  reset()
                }}
                disabled={!accountItems.length}
              >
                <option value="">{projectId ? (accounts.loading ? 'Loading…' : 'Pick an account…') : '—'}</option>
                {accountItems.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                    {a.aws_account_id ? ` (${a.aws_account_id})` : ''}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Environment</span>
              <select value={chosenEnvironment} onChange={(e) => setEnvironment(e.target.value)} required>
                <option value="">Select environment…</option>
                {ENVIRONMENTS.map((env) => (
                  <option key={env.value} value={env.value}>
                    {env.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {account && (
            <div className="row-3">
              <label className="field">
                <span>Region</span>
                <RegionSelect
                  value={region}
                  onChange={(value) => {
                    setRegion(value)
                    setListedRegion(value)
                    reset()
                  }}
                  emptyLabel={account.default_region ? `Account default · ${account.default_region}` : "Watchly's own region"}
                />
              </label>
              <label className="field">
                <span>VPC</span>
                <select
                  value={awsVpcId}
                  onChange={(e) => {
                    setAwsVpcId(e.target.value)
                    setVpcId('')
                    setDiscovery(null)
                  }}
                  disabled={!available.data?.vpcs.length}
                >
                  <option value="">
                    {available.loading
                      ? 'Asking AWS…'
                      : available.data && !available.data.vpcs.length
                        ? `No VPC in ${available.data.region}`
                        : 'Pick a VPC…'}
                  </option>
                  {(available.data?.vpcs ?? []).map((v) => (
                    <option key={v.aws_vpc_id} value={v.aws_vpc_id}>
                      {v.name ?? v.aws_vpc_id} · {v.cidrs.join(', ')}
                      {v.registered_as ? ' · watched' : ''}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          )}
          <div className="form-actions">
            <button type="button" className="btn btn-primary" onClick={() => discover(Boolean(discovery))} disabled={!vpc || busy}>
              {busy && !picked.length ? 'Asking AWS…' : discovery ? 'Discover again' : 'Discover resources'}
            </button>
            {discovery && <span className="muted small">Listed {timeAgo(discovery.discovered_at)}</span>}
          </div>
        </section>
      )}

      {discovery && (
        <section className="card">
          <h2>In {vpc?.name ?? vpc?.aws_vpc_id}</h2>
          {discovery.skipped.length > 0 && (
            <div className="banner banner-info small">
              AWS refused some listings, so these may be missing:{' '}
              {discovery.skipped.map((s) => `${s.service}:${s.call} (${s.reason})`).join(', ')}
            </div>
          )}
          {!discovery.items.length ? (
            <p className="muted">No EC2 instances, Auto Scaling groups, or Application or Network Load Balancers in this VPC.</p>
          ) : (
            <ul className="discovered-list">
              {discovery.items.map((item) => (
                <Item
                  key={item.key}
                  item={item}
                  chosen={Boolean(chosen[item.key])}
                  onToggle={() => toggle(item)}
                  choice={choices[item.key] ?? { checks: {}, publicIp: false }}
                  onChoice={(choice) => setChoices({ ...choices, [item.key]: choice })}
                  diagnoseBase={{ vpc_id: Number(vpcId), project_id: Number(projectId) || undefined }}
                />
              ))}
            </ul>
          )}
          <div className="form-actions">
            <button type="button" className="btn btn-primary" onClick={save} disabled={busy || !picked.length}>
              {busy && picked.length ? 'Adding…' : `Add ${picked.length || ''} resource${picked.length === 1 ? '' : 's'}`}
            </button>
          </div>
        </section>
      )}
    </>
  )
}
