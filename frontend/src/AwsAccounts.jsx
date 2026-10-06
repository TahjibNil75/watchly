// An infrastructure project's AWS accounts: the fields for one (also used
// when the project is created), and the card on the project page that lists,
// adds, edits, tests and removes them. Mirrors /monitoring/infra/aws/accounts.
import { useState } from 'react'
import { api } from './api.js'
import { ConfirmDialog, EnvironmentBadge, Empty, ErrorBanner, Loading } from './components.jsx'
import { ENVIRONMENTS } from './environments.js'
import { timeAgo } from './format.js'
import { RegionSelect } from './Infra.jsx'
import { BLANK_ACCOUNT, credentialsLine, newAccountPayload } from './infra.js'
import { useApi } from './useApi.js'

export function Mark({ ok }) {
  return (
    <span className={`perm-mark ${ok === true ? 'is-ok' : ok === false ? 'is-failed' : 'is-unknown'}`} aria-hidden="true">
      {ok === true ? '✓' : ok === false ? '✗' : '?'}
    </span>
  )
}

// One account's fields. With `account`, it edits that stored account: the
// secret may be left blank to keep it, unless the key id changes.
export function AccountFields({ value: form, onChange, account = null }) {
  const set = (key) => (e) => onChange({ ...form, [key]: e.target.value })
  const accessKey = form.auth_type === 'access_key'
  const needsSecret =
    accessKey && (!account || account.auth_type !== 'access_key' || form.access_key_id.trim() !== account.access_key_id)
  return (
    <>
      <div className="row-2">
        <label className="field">
          <span>Account name</span>
          <input value={form.name} onChange={set('name')} placeholder="Production" maxLength={255} required />
        </label>
        <label className="field">
          <span>Default region</span>
          <RegionSelect value={form.default_region} onChange={(region) => onChange({ ...form, default_region: region })} emptyLabel="Watchly's own" />
        </label>
      </div>
      <div className="row-2">
        <label className="field">
          <span>Environment</span>
          <select value={form.environment} onChange={set('environment')}>
            <option value="">Not set</option>
            {ENVIRONMENTS.map((env) => (
              <option key={env.value} value={env.value}>
                {env.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>
            Description <span className="muted">(optional)</span>
          </span>
          <input value={form.description} onChange={set('description')} placeholder="Client A, billing account" />
        </label>
      </div>
      <span className="muted small">Credentials</span>
      <div className="segmented" role="group" aria-label="Credentials">
        <button type="button" aria-pressed={accessKey} onClick={() => onChange({ ...form, auth_type: 'access_key' })}>
          Access key
        </button>
        <button type="button" aria-pressed={!accessKey} onClick={() => onChange({ ...form, auth_type: 'default' })}>
          Watchly&apos;s own (instance role)
        </button>
      </div>
      {accessKey && (
        <div className="row-2">
          <label className="field">
            <span>Access key ID</span>
            <input
              value={form.access_key_id}
              onChange={set('access_key_id')}
              placeholder="AKIA…"
              required
              spellCheck={false}
              autoComplete="off"
            />
          </label>
          <label className="field">
            <span>Secret access key</span>
            <input
              type="password"
              value={form.secret_access_key}
              onChange={set('secret_access_key')}
              placeholder={needsSecret ? '' : `unchanged (${account.secret_access_key_hint ?? 'stored'})`}
              required={needsSecret}
              autoComplete="new-password"
            />
          </label>
        </div>
      )}
      <div className="row-2">
        <label className="field">
          <span>
            Role to assume <span className="muted">(optional)</span>
          </span>
          <input
            value={form.role_arn}
            onChange={set('role_arn')}
            placeholder="arn:aws:iam::123456789012:role/watchly-monitor"
            spellCheck={false}
          />
        </label>
        <label className="field">
          <span>
            External ID <span className="muted">(optional)</span>
          </span>
          <input value={form.external_id} onChange={set('external_id')} disabled={!form.role_arn.trim()} />
        </label>
      </div>
      <label className="check">
        <input
          type="checkbox"
          checked={Boolean(form.watch_deployments)}
          onChange={(e) => onChange({ ...form, watch_deployments: e.target.checked })}
        />
        <span>Watch CodeDeploy deployments</span>
      </label>
      <p className="muted small">
        Each deployment is announced (&ldquo;Deploying orders-api to production&rdquo;), and the servers, load
        balancers and Auto Scaling groups it deploys to send no down alerts until it ends. Needs
        codedeploy:ListDeployments, BatchGetDeployments and GetDeploymentGroup.
      </p>
      <label className="check">
        <input
          type="checkbox"
          checked={Boolean(form.watch_capacity)}
          onChange={(e) => onChange({ ...form, watch_capacity: e.target.checked })}
        />
        <span>Watch Elastic IPs and quota</span>
      </label>
      <p className="muted small">
        Hourly, in each region it has VPCs in: an alert for Elastic IPs attached to nothing (AWS bills each about
        US$3.65 a month) and for the Elastic IP quota nearly used. Needs ec2:DescribeAddresses and
        servicequotas:GetServiceQuota.
      </p>
    </>
  )
}

// "2 unattached · 4 of 5 Elastic IPs", over every region looked at.
function capacityLine(a) {
  const regions = a.capacity ?? []
  const unattached = regions.reduce((n, r) => n + r.unattached.length, 0)
  const fullest = regions
    .filter((r) => r.elastic_ip_quota)
    .reduce((best, r) => (best && best.elastic_ips / best.elastic_ip_quota >= r.elastic_ips / r.elastic_ip_quota ? best : r), null)
  const parts = []
  if (unattached) parts.push(`${unattached} unattached`)
  if (fullest) parts.push(`${fullest.elastic_ips} of ${fullest.elastic_ip_quota}${fullest.quota_source === 'assumed' ? '?' : ''} EIPs`)
  return parts.join(' · ')
}

// What each region holds, for the badge's tooltip.
function capacityTitle(a) {
  return (a.capacity ?? [])
    .map((r) => {
      const quota = r.elastic_ip_quota ? ` of ${r.elastic_ip_quota}${r.quota_source === 'assumed' ? ' (assumed)' : ''}` : ''
      const loose = r.unattached.length ? `; unattached: ${r.unattached.map((u) => u.public_ip).join(', ')}` : ''
      return `${r.region}: ${r.elastic_ips}${quota} Elastic IPs${loose}`
    })
    .join('\n')
}

function CapacityCell({ account: a }) {
  if (!a.watch_capacity) return <span className="muted">not watched</span>
  if (a.capacity_error)
    return (
      <span className="badge badge-down" title={a.capacity_error}>
        failing
      </span>
    )
  if (!a.capacity_checked_at) return <span className="badge badge-unknown">watched, not looked yet</span>
  const line = capacityLine(a)
  const unattached = (a.capacity ?? []).some((r) => r.unattached.length)
  return (
    <>
      <span
        className={`badge ${unattached ? 'badge-degraded' : 'badge-healthy'}`}
        title={`Looked ${timeAgo(a.capacity_checked_at)}\n${capacityTitle(a)}`}
      >
        watched
      </span>
      {line && <div className="muted small nowrap">{line}</div>}
    </>
  )
}

function AccountForm({ account, busy, onSave, onCancel }) {
  const [form, setForm] = useState(() =>
    account
      ? {
          ...Object.fromEntries(Object.keys(BLANK_ACCOUNT).map((k) => [k, account[k] ?? ''])),
          secret_access_key: '',
          watch_deployments: account.watch_deployments,
          watch_capacity: account.watch_capacity,
        }
      : BLANK_ACCOUNT,
  )

  function submit(event) {
    event.preventDefault()
    if (!account) {
      onSave(newAccountPayload(form))
      return
    }
    // Only what changed; a blank secret keeps the stored one.
    const payload = {}
    const accessKey = form.auth_type === 'access_key'
    for (const key of Object.keys(BLANK_ACCOUNT)) {
      if (typeof BLANK_ACCOUNT[key] === 'boolean') {
        if (form[key] !== account[key]) payload[key] = form[key]
        continue
      }
      const value = form[key].trim()
      if (!accessKey && (key === 'access_key_id' || key === 'secret_access_key')) continue
      if (key === 'secret_access_key' && !value) continue
      if (value === (account[key] ?? '')) continue
      payload[key] = value || null
    }
    if (form.auth_type !== account.auth_type) payload.auth_type = form.auth_type
    onSave(payload)
  }

  return (
    <form className="form" onSubmit={submit} autoComplete="off">
      <AccountFields value={form} onChange={setForm} account={account} />
      <p className="muted small">
        Watchly tries the credentials with AWS before saving them. The secret is stored encrypted and never shown
        again.
      </p>
      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? 'Asking AWS…' : account ? 'Save' : 'Add account'}
        </button>
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  )
}

// The project page's card. Only someone who may manage the project sees it:
// the accounts hold its credentials.
export function AwsAccountsCard({ projectId }) {
  const accounts = useApi(() => api.listAwsAccounts(projectId), [projectId])
  const [editing, setEditing] = useState(null) // 'new' or an account
  const [removing, setRemoving] = useState(null)
  const [tests, setTests] = useState({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function run(action) {
    setBusy(true)
    setError(null)
    try {
      await action()
      return true
    } catch (err) {
      setError(err)
      return false
    } finally {
      setBusy(false)
    }
  }

  const save = (payload) =>
    run(async () => {
      if (editing === 'new') await api.createAwsAccount({ ...payload, project_id: Number(projectId) })
      else await api.updateAwsAccount(editing.id, payload)
      setEditing(null)
      accounts.reload()
    })
  const test = (a) =>
    run(async () => {
      const result = await api.testAwsAccount(a.id)
      setTests({ ...tests, [a.id]: result })
      accounts.reload()
    })
  const remove = async () => {
    if (await run(() => api.deleteAwsAccount(removing.id))) {
      setRemoving(null)
      accounts.reload()
    }
  }

  const items = accounts.data?.items ?? []

  return (
    <section className="card">
      <h2>AWS accounts</h2>
      <p className="muted small">
        This project&apos;s resources are read from these accounts, each with its own credentials. It keeps at least
        one. Give each the read-only Describe* permissions in the AWS setup guide.
      </p>
      <ErrorBanner error={error ?? accounts.error} />
      {accounts.loading ? (
        <Loading />
      ) : items.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Account</th>
                <th>Credentials</th>
                <th>Environment</th>
                <th>Region</th>
                <th>VPCs</th>
                <th>Deployments</th>
                <th>Capacity</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((a) => (
                <tr key={a.id}>
                  <td>
                    <strong>{a.name}</strong>
                    <div className="muted small record nowrap">{a.aws_account_id ?? 'account id not read yet'}</div>
                    {a.description && <div className="muted small">{a.description}</div>}
                  </td>
                  <td className="small">
                    {credentialsLine(a)}
                    {a.secret_access_key_hint && <span className="muted"> · secret {a.secret_access_key_hint}</span>}
                  </td>
                  <td>
                    <EnvironmentBadge environment={a.environment} />
                    {!a.environment && <span className="muted">—</span>}
                  </td>
                  <td>{a.default_region ?? <span className="muted">Watchly&apos;s own</span>}</td>
                  <td className="num">{a.vpc_count}</td>
                  <td>
                    {!a.watch_deployments ? (
                      <span className="muted">not watched</span>
                    ) : a.deployments_error ? (
                      <span className="badge badge-down" title={a.deployments_error}>
                        CodeDeploy failing
                      </span>
                    ) : a.deployments_checked_at ? (
                      <span className="badge badge-healthy" title={`Asked ${timeAgo(a.deployments_checked_at)}`}>
                        watched
                      </span>
                    ) : (
                      <span className="badge badge-unknown">watched, not asked yet</span>
                    )}
                  </td>
                  <td>
                    <CapacityCell account={a} />
                  </td>
                  <td>
                    {a.last_error ? (
                      <span className="badge badge-down" title={a.last_error}>
                        failing
                      </span>
                    ) : a.verified_at ? (
                      <span className="badge badge-healthy" title={`Verified ${timeAgo(a.verified_at)}`}>
                        verified
                      </span>
                    ) : (
                      <span className="badge badge-unknown">untested</span>
                    )}
                  </td>
                  <td>
                    <div className="actions">
                      <button type="button" className="btn btn-sm" onClick={() => test(a)} disabled={busy}>
                        Test
                      </button>
                      <button type="button" className="btn btn-sm" onClick={() => setEditing(a)} disabled={busy}>
                        Edit
                      </button>
                      <button
                        type="button"
                        className="btn btn-sm"
                        onClick={() => setRemoving(a)}
                        disabled={busy || a.vpc_count > 0 || items.length === 1}
                        title={
                          a.vpc_count > 0
                            ? 'Remove its resources first'
                            : items.length === 1
                              ? 'A project keeps at least one account'
                              : undefined
                        }
                      >
                        Remove
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty>No AWS account yet.</Empty>
      )}

      {Object.entries(tests).map(([id, t]) => {
        const a = items.find((x) => String(x.id) === id)
        if (!a || !t) return null
        return (
          <div key={id} className="account-test">
            <h3>
              {a.name} · {t.region}{' '}
              <button type="button" className="btn btn-sm" onClick={() => setTests({ ...tests, [id]: undefined })}>
                Hide
              </button>
            </h3>
            <p className="small record">
              {t.caller ? t.caller.arn : <span className="text-down">{t.caller_error}</span>}
            </p>
            <ul className="perm-list">
              {t.permissions.map((p) => (
                <li key={p.action}>
                  <Mark ok={p.ok} />
                  <code>{p.action}</code>
                  {p.detail && <span className="muted small"> · {p.detail}</span>}
                </li>
              ))}
            </ul>
          </div>
        )
      })}

      {editing ? (
        <>
          <h3>{editing === 'new' ? 'Add an AWS account' : `Edit ${editing.name}`}</h3>
          <AccountForm
            key={editing === 'new' ? 'new' : editing.id}
            account={editing === 'new' ? null : editing}
            busy={busy}
            onSave={save}
            onCancel={() => setEditing(null)}
          />
        </>
      ) : (
        <div className="form-actions">
          <button type="button" className="btn" onClick={() => setEditing('new')}>
            Add another AWS account
          </button>
        </div>
      )}

      {removing && (
        <ConfirmDialog
          title={`Remove ${removing.name}?`}
          word="remove"
          confirmLabel="Remove"
          busy={busy}
          error={error}
          onConfirm={remove}
          onCancel={() => setRemoving(null)}
        >
          Its credentials are deleted from Watchly. Nothing changes in AWS.
        </ConfirmDialog>
      )}
    </section>
  )
}
