import { useState } from 'react'
import { ErrorBanner } from './components.jsx'
import { CHECKS_BY_KIND, CHECK_TYPES, PUBLIC_IP_CHECKS } from './infra.js'

// A server's check goes to its private IP unless this is ticked; a group's,
// to each instance's.
const PUBLIC_IP_FIELD = {
  key: 'use_public_ip',
  label: (resource) =>
    resource.kind === 'auto_scaling_group'
      ? "Check each instance's public IP over the internet"
      : `Check its public IP (${resource.aws_detail?.public_ip ?? 'none now'}) over the internet`,
  type: 'checkbox',
  initial: false,
  note: 'Instead of the private IP, from inside the VPC.',
}

// An Auto Scaling group's ping, tcp and http run on every instance in service.
const MIN_INSTANCES_FIELD = {
  key: 'min_healthy_instances',
  label: 'Down when fewer instances pass than',
  type: 'number',
  min: 1,
  max: 1000,
  initial: '',
  nullable: true,
  note: 'Runs on every instance in service. Blank: down only when none passes; any failing one is a problem.',
}

// Each type's settings, as the API's models in infra/aws/schemas.py take
// them, with the same defaults. `nullable` numbers may be left blank: that
// turns the problem off.
const FIELDS = {
  ping: [
    { key: 'count', label: 'Pings per check', type: 'number', min: 1, max: 20, initial: 3 },
    {
      key: 'packet_loss_threshold_percent',
      label: 'Packet loss counts as a problem at (%)',
      type: 'number',
      min: 1,
      max: 100,
      initial: 20,
      nullable: true,
    },
  ],
  tcp: [
    { key: 'port', label: 'Port', type: 'number', min: 1, max: 65535, initial: '', required: true },
    { key: 'tls', label: 'Also complete a TLS handshake', type: 'checkbox', initial: false },
    { key: 'expect_banner', label: 'Banner must contain', type: 'text', initial: '', note: 'e.g. SSH; blank reads none.' },
  ],
  http: [
    { key: 'scheme', label: 'Scheme', type: 'select', options: ['http', 'https'], initial: 'http' },
    { key: 'port', label: 'Port', type: 'number', min: 1, max: 65535, initial: '', note: '80 or 443 when blank.' },
    { key: 'path', label: 'Path', type: 'text', initial: '/health' },
    { key: 'host_header', label: 'Host header', type: 'text', initial: '', note: 'For ALB rules that route by host.' },
    { key: 'expected_status', label: 'Expected status', type: 'number', min: 100, max: 599, initial: 200 },
    { key: 'must_contain', label: 'Body must contain', type: 'text', initial: '' },
    { key: 'slow_threshold_ms', label: 'Slow above (ms)', type: 'number', min: 1, initial: 3000, nullable: true },
    { key: 'verify_tls', label: 'Verify the certificate', type: 'checkbox', initial: false },
  ],
  target_health: [
    { key: 'target_group_arn', label: 'Target group', type: 'target_group', initial: '' },
    {
      key: 'min_healthy_targets',
      label: 'Down below this many healthy targets',
      type: 'number',
      min: 0,
      initial: 1,
      note: 'Targets that are registering or draining count as neither healthy nor failing.',
    },
  ],
  group_health: [
    {
      key: 'min_healthy_instances',
      label: 'Down below this many healthy instances',
      type: 'number',
      min: 0,
      max: 1000,
      initial: 1,
      note: 'Never more than the desired capacity. Short of desired capacity, or an unhealthy instance, is a problem.',
    },
  ],
  // Nothing to set: down unless RDS says it is available, or busy with
  // something it serves through.
  db_status: [],
  db_metrics: [
    { key: 'cpu_percent_max', label: 'CPU counts as a problem above (%)', type: 'number', min: 1, max: 100, initial: 90, nullable: true },
    {
      key: 'free_storage_percent_min',
      label: 'Free storage counts as a problem below (%)',
      type: 'number',
      min: 1,
      max: 99,
      initial: 10,
      nullable: true,
      note: 'Of its allocated storage. Not read for Aurora, whose storage grows by itself.',
    },
    { key: 'freeable_memory_mb_min', label: 'Freeable memory counts as a problem below (MB)', type: 'number', min: 1, initial: '', nullable: true },
    { key: 'connections_max', label: 'Connections count as a problem above', type: 'number', min: 1, initial: '', nullable: true },
    {
      key: 'replica_lag_seconds_max',
      label: 'Replica lag counts as a problem above (s)',
      type: 'number',
      min: 1,
      initial: 60,
      nullable: true,
      note: 'For a read replica only.',
    },
  ],
}

// The fields a check of `type` shows for this resource: for a group's
// instance check, how many must pass; the public-IP choice only where there
// is a public IP, or the check already uses it.
function fieldsFor(type, resource, check) {
  const fields = [...FIELDS[type]]
  if (!PUBLIC_IP_CHECKS.has(type)) return fields
  const group = resource.kind === 'auto_scaling_group'
  if (group) fields.unshift(MIN_INSTANCES_FIELD)
  const publicIp = group
    ? (resource.aws_detail?.instances ?? []).some((i) => i.public_ip)
    : resource.aws_detail?.public_ip
  if ((resource.kind === 'server' || group) && (publicIp || check?.settings?.use_public_ip)) fields.push(PUBLIC_IP_FIELD)
  return fields
}

// A new check starts from the defaults; an existing one from its settings,
// where null (a problem turned off) shows as blank.
function initialSettings(fields, settings) {
  return Object.fromEntries(
    fields.map((f) => {
      if (!settings) return [f.key, f.initial]
      const value = settings[f.key]
      if (value === null || value === undefined) return [f.key, f.type === 'checkbox' ? false : '']
      return [f.key, value]
    }),
  )
}

// What the API takes: blanks dropped (so its defaults apply), or null for a
// problem left off.
function toSettings(fields, values) {
  const out = {}
  for (const f of fields) {
    const value = values[f.key]
    if (f.type === 'checkbox') out[f.key] = Boolean(value)
    else if (value === '' || value === undefined) {
      if (f.nullable) out[f.key] = null
    } else if (f.type === 'number') out[f.key] = Number(value)
    else out[f.key] = value
  }
  return out
}

export default function InfraCheckForm({ resource, check, onSubmit, onCancel }) {
  const editing = Boolean(check)
  const allowed = CHECKS_BY_KIND[resource.kind]
  const [type, setType] = useState(check?.check_type ?? allowed[0])
  const fields = fieldsFor(type, resource, check)
  const [values, setValues] = useState(() => initialSettings(fields, check?.settings))
  const [name, setName] = useState(check?.name ?? '')
  const [every, setEvery] = useState(check?.check_interval_seconds ?? 300)
  const [timeoutSeconds, setTimeoutSeconds] = useState(check?.timeout_seconds ?? '')
  const [retries, setRetries] = useState(check?.retries_on_failure ?? 1)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const pickType = (next) => {
    setType(next)
    setValues(initialSettings(fieldsFor(next, resource)))
  }
  const set = (key) => (e) =>
    setValues({ ...values, [key]: e.target.type === 'checkbox' ? e.target.checked : e.target.value })

  const groups = resource.aws_detail?.target_groups ?? []
  const listenerPorts = (resource.aws_detail?.listeners ?? []).map((l) => `${l.protocol}:${l.port}`)

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    const payload = {
      name: name.trim() || undefined,
      settings: toSettings(fields, values),
      check_interval_seconds: Number(every),
      retries_on_failure: Number(retries),
      ...(timeoutSeconds !== '' ? { timeout_seconds: Number(timeoutSeconds) } : {}),
    }
    if (!editing) payload.check_type = type
    try {
      await onSubmit(payload)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="form check-form" onSubmit={submit}>
      <ErrorBanner error={error} />
      <div className="row-2">
        <label className="field">
          <span>Type</span>
          <select value={type} onChange={(e) => pickType(e.target.value)} disabled={editing}>
            {allowed.map((t) => (
              <option key={t} value={t}>
                {CHECK_TYPES[t].label} — {CHECK_TYPES[t].note}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Name</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Named after its settings when blank" maxLength={255} />
        </label>
      </div>

      <div className="check-fields">
        {fields.map((f) => (
          <label key={f.key} className={`field${f.type === 'checkbox' ? ' field-check' : ''}`}>
            {f.type === 'checkbox' ? (
              <>
                <input type="checkbox" checked={Boolean(values[f.key])} onChange={set(f.key)} />
                <span>{typeof f.label === 'function' ? f.label(resource) : f.label}</span>
              </>
            ) : (
              <>
                <span>{f.label}</span>
                {f.type === 'select' ? (
                  <select value={values[f.key]} onChange={set(f.key)}>
                    {f.options.map((o) => (
                      <option key={o}>{o}</option>
                    ))}
                  </select>
                ) : f.type === 'target_group' ? (
                  groups.length ? (
                    <select value={values[f.key]} onChange={set(f.key)} required>
                      <option value="">Pick a target group…</option>
                      {groups.map((g) => (
                        <option key={g.arn} value={g.arn}>
                          {g.name} · {g.health_check}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input value={values[f.key]} onChange={set(f.key)} placeholder="arn:aws:elasticloadbalancing:…" required />
                  )
                ) : (
                  <input
                    type={f.type}
                    value={values[f.key]}
                    onChange={set(f.key)}
                    min={f.min}
                    max={f.max}
                    required={f.required}
                    placeholder={f.nullable ? 'off' : undefined}
                  />
                )}
                {f.note && <small className="muted">{f.note}</small>}
                {f.key === 'port' && type === 'tcp' && listenerPorts.length > 0 && (
                  <small className="muted">Its listeners: {listenerPorts.join(', ')}</small>
                )}
                {f.key === 'port' && type === 'tcp' && resource.kind === 'database' && resource.aws_detail?.port && (
                  <small className="muted">
                    Its endpoint listens on {resource.aws_detail.port}. Opens and closes a connection; never logs in.
                  </small>
                )}
              </>
            )}
          </label>
        ))}
      </div>

      <div className="row-3">
        <label className="field">
          <span>Check every (seconds)</span>
          <input type="number" min={30} max={86400} value={every} onChange={(e) => setEvery(e.target.value)} />
        </label>
        <label className="field">
          <span>Timeout (seconds)</span>
          <input type="number" min={1} max={120} value={timeoutSeconds} onChange={(e) => setTimeoutSeconds(e.target.value)} placeholder={type === 'ping' ? '2' : '10'} />
        </label>
        <label className="field">
          <span>Retries before it counts</span>
          <input type="number" min={0} max={3} value={retries} onChange={(e) => setRetries(e.target.value)} />
        </label>
      </div>

      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? 'Saving…' : editing ? 'Save check' : 'Add check'}
        </button>
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  )
}
