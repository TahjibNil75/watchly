// The Infrastructure page's setup checklist: what each resource still needs
// before Watchly can watch it (a security group rule, a listening port, an
// IAM permission, the CloudWatch agent, a check to add). Read from
// GET /readiness, scoped like the rest of the page. It is a button (with the
// steps left on it) that opens the list in a dialog, so it stays off the page.
import { useEffect, useId, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from './api.js'
import { KindIcon, StateBadge } from './Infra.jsx'
import { useApi } from './useApi.js'

const SHOW_READY_KEY = 'watchly.infraChecklistReady'

const MARKS = { done: '✓', todo: '!', unknown: '?', optional: '·' }
const STATUS_LABELS = { done: 'Done', todo: 'To do', unknown: 'Not known', optional: 'Optional' }
const AREA_LABELS = {
  watchly: 'Watchly',
  account: 'AWS account',
  vpc: 'VPC',
  checks: 'Checks',
  network: 'Security group / network',
  service: 'Service',
  permission: 'IAM permission',
  agent: 'CloudWatch agent',
}

function remembered(fallback) {
  try {
    const saved = localStorage.getItem(SHOW_READY_KEY)
    return saved === null ? fallback : saved === '1'
  } catch {
    return fallback
  }
}

function remember(value) {
  try {
    localStorage.setItem(SHOW_READY_KEY, value ? '1' : '0')
  } catch {
    // Remembering it is a nicety.
  }
}

function Step({ step, resourceId }) {
  return (
    <li className={`ready-step is-${step.status}`}>
      <span className="ready-mark" title={STATUS_LABELS[step.status]} aria-label={STATUS_LABELS[step.status]}>
        {MARKS[step.status]}
      </span>
      <div className="ready-step-text">
        <div>
          <span className="ready-area">{AREA_LABELS[step.area] ?? step.area}</span>
          <strong>{step.title}</strong>
          {step.aws_ref && <code className="ready-ref">{step.aws_ref}</code>}
        </div>
        {step.detail && <div className="muted small">{step.detail}</div>}
        {step.fix && step.status !== 'done' && (
          <div className="ready-fix small">
            <strong>To do:</strong> {step.fix}
            {step.check_id && resourceId && (
              <>
                {' '}
                <Link to={`/infra/resources/${resourceId}`}>Open the check</Link>
              </>
            )}
          </div>
        )}
      </div>
    </li>
  )
}

// The checklist by VPC: the VPC's own step, then its resources. A VPC with
// nothing to show (all ready, ready ones hidden) is left out.
function groupByVpc(data, showReady) {
  const groups = new Map()
  const group = (vpc) => {
    if (!groups.has(vpc.id)) groups.set(vpc.id, { vpc, steps: [], rows: [] })
    return groups.get(vpc.id)
  }
  for (const row of data.resources) {
    if (showReady || row.todo > 0) group(row.resource.vpc).rows.push(row)
  }
  const names = new Map(data.resources.map((row) => [row.resource.vpc.id, row.resource.vpc]))
  for (const step of data.general) {
    if (step.vpc_id == null) continue
    if (showReady || step.status !== 'done' || groups.has(step.vpc_id)) {
      group(names.get(step.vpc_id) ?? { id: step.vpc_id, name: step.aws_ref ?? 'VPC' }).steps.push(step)
    }
  }
  return [...groups.values()].sort((a, b) => a.vpc.name.localeCompare(b.vpc.name))
}

function VpcGroup({ group }) {
  const todo = group.steps.filter((s) => s.status === 'todo').length + group.rows.reduce((n, row) => n + row.todo, 0)
  return (
    <section className="ready-vpc">
      <header className="ready-vpc-head">
        <Link to={`/infra/vpcs/${group.vpc.id}`} className="strong-link">
          {group.vpc.name}
        </Link>
        <span className="muted small">
          {group.rows.length} resource{group.rows.length === 1 ? '' : 's'}
        </span>
        <span className={`ready-count small ${todo ? 'text-down' : 'text-up'}`}>{todo ? `${todo} to do` : 'Ready'}</span>
      </header>
      {group.steps.length > 0 && (
        <ol className="ready-steps ready-general">
          {group.steps.map((s, i) => (
            <Step key={`${s.area}-${i}`} step={s} />
          ))}
        </ol>
      )}
      <div className="ready-resources">
        {group.rows.map((row) => (
          <ResourceRow key={row.resource.id} row={row} />
        ))}
      </div>
    </section>
  )
}

function ResourceRow({ row }) {
  const r = row.resource
  // The steps left first; the done ones are there to show what was checked.
  const order = { todo: 0, unknown: 1, optional: 2, done: 3 }
  const steps = [...row.steps].sort((a, b) => order[a.status] - order[b.status])
  return (
    <details className={`ready-resource${row.todo ? ' has-todo' : ''}`} open={row.todo > 0 && row.todo <= 3}>
      <summary>
        <KindIcon kind={r.kind} className="ready-kind" />
        <Link to={`/infra/resources/${r.id}`} className="strong-link" onClick={(e) => e.stopPropagation()}>
          {r.name}
        </Link>
        <StateBadge state={row.state} />
        <span className={`ready-count small ${row.todo ? 'text-down' : 'text-up'}`}>
          {row.todo ? `${row.todo} to do` : 'Ready'}
        </span>
      </summary>
      <ol className="ready-steps">
        {steps.map((s, i) => (
          <Step key={`${s.area}-${s.check_id ?? ''}-${i}`} step={s} resourceId={r.id} />
        ))}
      </ol>
    </details>
  )
}

const ChecklistIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M9 11l3 3 8-8" />
    <path d="M20 12v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9" />
  </svg>
)

export default function SetupChecklist({ scope, enabled }) {
  const [open, setOpen] = useState(false)
  const [showReady, setShowReadyState] = useState(() => remembered(false))
  const titleId = useId()
  const setShowReady = (value) => {
    setShowReadyState(value)
    remember(value)
  }
  const { kind, vpc_id: vpcId, q, environment } = scope
  // The security groups are read from AWS, so this polls slowly.
  const readiness = useApi(() => (enabled ? api.infraReadiness(scope) : null), [enabled, kind, vpcId, q, environment], {
    pollMs: 120000,
  })

  useEffect(() => {
    if (!open) return undefined
    const onKey = (e) => e.key === 'Escape' && setOpen(false)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  const data = readiness.data
  // The answer reads security groups from AWS and can take a while, so the
  // button is there at once, waiting, rather than popping in and shoving the
  // buttons beside it along. It goes only if there turns out to be nothing to set up.
  if (!data) {
    return readiness.error ? null : (
      <button type="button" className="btn btn-purple ready-button" disabled aria-busy="true">
        <ChecklistIcon />
        Setup checklist
        <span className="ready-badge">…</span>
      </button>
    )
  }
  if (data.total === 0 && !data.general.some((s) => s.status === 'todo')) return null

  const generalTodo = data.general.filter((s) => s.status === 'todo').length
  const stepsLeft = generalTodo + data.resources.reduce((n, row) => n + row.todo, 0)
  const general = data.general.filter((s) => s.vpc_id == null)
  const groups = groupByVpc(data, showReady)
  const allSet = stepsLeft === 0

  return (
    <>
      <button
        type="button"
        className="btn btn-purple ready-button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        title={`${data.ready} of ${data.total} resources ready to watch`}
      >
        <ChecklistIcon />
        Setup checklist
        <span className={`ready-badge ${allSet ? 'is-ready' : 'has-todo'}`}>{allSet ? '✓' : stepsLeft}</span>
      </button>

      {open && (
        <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && setOpen(false)}>
          <section className="modal card ready-modal" role="dialog" aria-modal="true" aria-labelledby={titleId}>
            <header className="ready-modal-head">
              <div className="ready-head-text">
                <h2 id={titleId}>Setup checklist</h2>
                <span className="muted small">
                  {data.ready} of {data.total} resources ready to watch
                  {allSet ? ' · nothing left to do' : ` · ${stepsLeft} step${stepsLeft === 1 ? '' : 's'} left`}
                </span>
              </div>
              <button type="button" className="btn btn-sm ready-close" onClick={() => setOpen(false)}>
                Close
              </button>
            </header>
            <div className="ready-meter" aria-hidden="true">
              <i style={{ width: `${data.total ? (100 * data.ready) / data.total : 100}%` }} />
            </div>

            <div className="ready-body">
              <p className="muted small ready-intro">
                What each resource needs before Watchly can reach and read it: the security group rules for its
                ping, TCP and HTTP checks, a service listening on the port, the IAM permissions its AWS checks use.
                {!data.security_groups_read &&
                  ' Security groups were not read (they are, for Admin and DevOps, when Watchly runs on EC2), so these steps come from the checks’ last results.'}
              </p>

              {general.length > 0 && (
                <ol className="ready-steps ready-general">
                  {general.map((s, i) => (
                    <Step key={`${s.area}-${i}`} step={s} />
                  ))}
                </ol>
              )}

              {groups.length > 0 ? (
                groups.map((group) => <VpcGroup key={group.vpc.id} group={group} />)
              ) : (
                <p className="muted small">Every resource here can be watched.</p>
              )}
            </div>

            <div className="ready-foot">
              <label className="small">
                <input type="checkbox" checked={showReady} onChange={(e) => setShowReady(e.target.checked)} /> Show
                ready resources too
              </label>
              <button type="button" className="btn btn-sm btn-purple" onClick={readiness.reload}>
                Re-check
              </button>
            </div>
          </section>
        </div>
      )}
    </>
  )
}
