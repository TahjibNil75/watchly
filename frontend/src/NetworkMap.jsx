// The network maps. VpcMap draws one VPC from GET /vpcs/{id}/topology: its
// subnets as lanes, load balancers → target groups → instances, databases in
// the subnet of their AZ, and Watchly's
// own paths in (probes inside the VPC or over the internet, and what it reads
// from the AWS API). InfraMap draws every monitored resource of every VPC on
// one canvas from what the resources already carry, without reading AWS: the
// internet and Watchly outside, each VPC a frame of load balancers, Auto
// Scaling groups and subnet lanes, wired the way traffic flows.
//
// Nodes are laid out with CSS grid and flex; the links are one SVG over the
// top, drawn from where the nodes ended up and measured again on every resize.
import { useCallback, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { EnvironmentBadge } from './components.jsx'
import { since, timeAgo } from './format.js'
import { InfraIcon, KindIcon, StateBadge } from './Infra.jsx'
import { exposureLine, groupInstances, groupServices, groupState, groupSummary, isPublic, kindInfo, resourceTrouble, stateLabel } from './infra.js'

// --- tones ------------------------------------------------------------------

const STATE_TONE = {
  healthy: 'healthy',
  degraded: 'degraded',
  down: 'down',
  unknown: 'unknown',
  maintenance: 'maintenance',
  paused: 'paused',
  missing: 'down',
}
const AWS_TONE = {
  healthy: 'healthy',
  degraded: 'degraded',
  transition: 'degraded',
  unhealthy: 'down',
  unknown: 'unknown',
  empty: 'unknown',
}
const STOPPED = new Set(['stopped', 'stopping', 'terminated', 'shutting-down'])

// A monitored node shows its state; anything else what AWS says of it.
function toneOf(node) {
  if (node.kind === 'watchly') return 'primary'
  if (node.state) return STATE_TONE[node.state] ?? 'unknown'
  if (STOPPED.has(node.aws_state)) return 'paused'
  return AWS_TONE[node.aws_health] ?? 'none'
}

const EDGE_RANK = { failing: 0, degraded: 1, unknown: 2, ok: 3, off: 4 }

// --- families: which path a link is on ----------------------------------------
//
// A link's colour says what kind of path it is, and the node it leads to wears
// the same colour: the load balancer path (internet → load balancer → target
// group → target), servers and the Auto Scaling groups that run them, and
// bastions, the instances reached straight from the internet. Three hues,
// checked as a set for both themes and for colour-blind readers; a fourth would
// not pass, so databases and AWS API reads stay neutral and are told apart by
// their line. Trouble wears the status colours over the family: red for
// failing, grey for off; a degraded link keeps its colour and dashes.
const FAMILY_LABEL = {
  lb: 'Load balancer path',
  server: 'Servers & Auto Scaling',
  bastion: 'Bastion · public instance',
  db: 'Database',
  neutral: 'AWS API & other',
}
const LINK_TONES = ['lb', 'server', 'bastion', 'db', 'neutral', 'degraded', 'failing', 'off']

function nodeFamily(node) {
  switch (node?.kind) {
    case 'load_balancer':
    case 'target_group':
      return 'lb'
    case 'instance':
      return node.public ? 'bastion' : 'server'
    case 'ip_target':
    case 'auto_scaling_group':
      return 'server'
    case 'database':
      return 'db'
    default:
      return 'neutral'
  }
}

function resourceFamily(r) {
  switch (r.kind) {
    case 'load_balancer':
      return 'lb'
    case 'server':
      return isPublic(r) ? 'bastion' : 'server'
    case 'auto_scaling_group':
      return 'server'
    case 'database':
      return 'db'
    default:
      return 'neutral'
  }
}

// Traffic is coloured by the path it is on; a check by what it checks.
function edgeFamily(edge, nodeOf) {
  switch (edge.kind) {
    case 'forwards':
    case 'targets':
      return 'lb'
    case 'launches':
    case 'registers':
      return 'server'
    case 'uses':
      return 'db'
    case 'api':
      return 'neutral'
    default:
      return nodeOf(edge.target) ?? 'neutral'
  }
}

function linkTone(edge) {
  if (edge.state === 'failing' || edge.state === 'off') return edge.state
  return edge.family ?? 'neutral'
}

// --- icons ------------------------------------------------------------------

const ICONS = {
  internet: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M3.5 12h17M12 3.5c2.4 2.6 3.5 5.4 3.5 8.5s-1.1 5.9-3.5 8.5c-2.4-2.6-3.5-5.4-3.5-8.5S9.6 6.1 12 3.5Z" />
    </>
  ),
  watchly: (
    <>
      <path d="M2.5 12s3.5-6.5 9.5-6.5 9.5 6.5 9.5 6.5-3.5 6.5-9.5 6.5S2.5 12 2.5 12Z" />
      <circle cx="12" cy="12" r="2.8" />
    </>
  ),
  aws_api: (
    <>
      <path d="M7 18.5h10.5a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.6 9.1 4.75 4.75 0 0 0 7 18.5Z" />
      <path d="M10 13.5h4M12 11.5v4" />
    </>
  ),
  target_group: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <circle cx="12" cy="12" r="4.5" />
      <circle cx="12" cy="12" r="0.8" />
    </>
  ),
  ip_target: (
    <>
      <rect x="4" y="6" width="16" height="12" rx="2" />
      <path d="M8 10v4M11 14v-4h2a1.5 1.5 0 0 1 0 3h-2" />
    </>
  ),
  account: (
    <>
      <rect x="3.5" y="5" width="17" height="14" rx="2" />
      <path d="M3.5 9.5h17M7 14h4" />
    </>
  ),
  lock: (
    <>
      <rect x="5" y="10.5" width="14" height="10" rx="2" />
      <path d="M8 10.5V8a4 4 0 0 1 8 0v2.5M12 14.5v2.5" />
    </>
  ),
  vpc: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2.5" strokeDasharray="3 2.5" />
      <rect x="7" y="8" width="4" height="4" rx="0.8" />
      <rect x="13" y="12" width="4" height="4" rx="0.8" />
    </>
  ),
}
const KIND_OF_NODE = {
  instance: 'server',
  load_balancer: 'load_balancer',
  auto_scaling_group: 'auto_scaling_group',
  database: 'database',
}
// The nodes that are AWS resources Watchly can monitor.
const RESOURCE_NODES = new Set(Object.keys(KIND_OF_NODE))

export function NodeIcon({ kind }) {
  if (KIND_OF_NODE[kind]) return <KindIcon kind={KIND_OF_NODE[kind]} />
  return <InfraIcon>{ICONS[kind]}</InfraIcon>
}

const NODE_KIND_LABEL = {
  internet: 'Internet',
  watchly: 'This monitoring server',
  aws_api: 'AWS API',
  load_balancer: 'Load balancer',
  target_group: 'Target group',
  auto_scaling_group: 'Auto Scaling group',
  instance: 'EC2 instance',
  ip_target: 'IP target',
  database: 'RDS database',
}

// --- the engine: measure nodes, draw links ------------------------------------

// Refs for every node by id, and their boxes relative to the canvas, measured
// after layout and again whenever the canvas changes size.
function useMeasure(deps) {
  const canvasRef = useRef(null)
  const elements = useRef(new Map())
  const callbacks = useRef(new Map())
  const [geometry, setGeometry] = useState({ rects: {}, width: 0, height: 0 })

  const nodeRef = useCallback((id) => {
    let callback = callbacks.current.get(id)
    if (!callback) {
      callback = (el) => {
        if (el) elements.current.set(id, el)
        else elements.current.delete(id)
      }
      callbacks.current.set(id, callback)
    }
    return callback
  }, [])

  useLayoutEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return undefined
    let frame = 0
    const measure = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        const base = canvas.getBoundingClientRect()
        const rects = {}
        for (const [id, el] of elements.current) {
          const r = el.getBoundingClientRect()
          rects[id] = { x: r.left - base.left, y: r.top - base.top, w: r.width, h: r.height }
        }
        setGeometry({ rects, width: canvas.offsetWidth, height: canvas.offsetHeight })
      })
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(canvas)
    return () => {
      observer.disconnect()
      cancelAnimationFrame(frame)
    }
    // The caller's deps say when the nodes may have moved.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { canvasRef, nodeRef, geometry }
}

// A curve from one box to another: out of the side facing it, or, for two in
// the same column, out of the right side and back in.
function linkPath(a, b) {
  const ay = a.y + a.h / 2
  const by = b.y + b.h / 2
  const acx = a.x + a.w / 2
  const bcx = b.x + b.w / 2
  if (bcx - acx > 24) {
    const [x1, x2] = [a.x + a.w, b.x - 3]
    const d = Math.max(28, (x2 - x1) / 2)
    return `M${x1},${ay} C${x1 + d},${ay} ${x2 - d},${by} ${x2},${by}`
  }
  if (acx - bcx > 24) {
    const [x1, x2] = [a.x, b.x + b.w + 3]
    const d = Math.max(28, (x1 - x2) / 2)
    return `M${x1},${ay} C${x1 - d},${ay} ${x2 + d},${by} ${x2},${by}`
  }
  const [x1, x2] = [a.x + a.w, b.x + b.w + 3]
  const d = 32 + Math.abs(by - ay) / 5
  return `M${x1},${ay} C${x1 + d},${ay} ${x2 + d},${by} ${x2},${by}`
}

function Links({ edges, geometry, focus }) {
  const prefix = useId().replace(/:/g, '')
  const { rects, width, height } = geometry
  // Quiet links first, so trouble is drawn on top.
  const ordered = [...edges].sort((x, y) => EDGE_RANK[y.state] - EDGE_RANK[x.state])
  return (
    <svg className="map-links" width={width} height={height} aria-hidden="true">
      <defs>
        {LINK_TONES.map((t) => (
          <marker
            key={t}
            id={`${prefix}-${t}`}
            viewBox="0 0 10 10"
            refX="8.5"
            refY="5"
            markerWidth="6.5"
            markerHeight="6.5"
            orient="auto-start-reverse"
          >
            <path d="M1,1.2 L9,5 L1,8.8 L3,5 z" className={`map-arrow arrow-${t}`} />
          </marker>
        ))}
      </defs>
      {ordered.map((e) => {
        const a = rects[e.source]
        const b = rects[e.target]
        if (!a || !b) return null
        const touches = focus && (e.source === focus || e.target === focus)
        const lit = focus && (touches ? 'is-lit' : 'is-dim')
        const tone = linkTone(e)
        const d = linkPath(a, b)
        const classes = [
          'map-link',
          `tone-${tone}`,
          `link-${e.state}`,
          `link-${e.kind}`,
          e.via === 'internet' && 'via-internet',
          e.state === 'ok' && (e.kind === 'probes' || touches) && 'is-flow',
          lit,
        ]
        return (
          <g key={`${e.source}>${e.target}>${e.kind}>${e.via ?? ''}`}>
            {/* A band of the canvas under each link, so crossings stay readable. */}
            <path d={d} className={['map-link-halo', lit].filter(Boolean).join(' ')} />
            <path
              d={d}
              className={classes.filter(Boolean).join(' ')}
              markerEnd={e.kind === 'internet' ? undefined : `url(#${prefix}-${e.state === 'degraded' ? 'degraded' : tone})`}
            />
          </g>
        )
      })}
    </svg>
  )
}

function MapNode({ node, nodeRef, sub, tone, family, focus, related, selected, faded, onSelect, onHover, to, children }) {
  const classes = [
    'map-node',
    `map-node-${node.kind}`,
    `mt-${tone}`,
    family && `fam-${family}`,
    children && 'has-chips',
    faded && !focus && 'is-faded',
    node.monitored === false && 'is-unmonitored',
    STOPPED.has(node.aws_state) && 'is-stopped',
    selected && 'is-selected',
    focus && (related ? 'is-lit' : 'is-dim'),
  ]
  const body = (
    <>
      <span className="map-node-icon">
        <NodeIcon kind={node.kind} />
      </span>
      <span className="map-node-text">
        <span className="map-node-label">{node.label}</span>
        {[sub].flat().filter(Boolean).map((line) => (
          <span key={line} className="map-node-sub" title={line}>
            {line}
          </span>
        ))}
      </span>
      <span className={`map-dot${tone === 'down' ? ' is-alarm' : ''}`} />
      {children}
    </>
  )
  const handlers = {
    ref: nodeRef(node.id),
    className: classes.filter(Boolean).join(' '),
    onMouseEnter: () => onHover?.(node.id),
    onMouseLeave: () => onHover?.(null),
    onFocus: () => onHover?.(node.id),
    onBlur: () => onHover?.(null),
    title: node.title,
  }
  if (to) {
    return (
      <Link to={to} {...handlers}>
        {body}
      </Link>
    )
  }
  return (
    <button type="button" {...handlers} onClick={() => onSelect?.(node.id)} aria-pressed={selected}>
      {body}
    </button>
  )
}

// Groups of [class, label]: a `mt-` class draws a state dot, anything else a
// sample of link.
function Legend({ groups }) {
  return (
    <div className="map-legend small">
      {groups.map(([title, items]) => (
        <div key={title} className="map-legend-group">
          <span className="map-legend-title">{title}</span>
          {items.map(([kind, label]) => (
            <span key={label} className="map-legend-item">
              {kind.startsWith('mt-') ? (
                <span className={`map-dot ${kind}`} />
              ) : (
                <svg width="30" height="10" aria-hidden="true">
                  <path d="M2,5 H28" className={`map-link ${kind}`} />
                </svg>
              )}
              {label}
            </span>
          ))}
        </div>
      ))}
    </div>
  )
}

const FAMILY_LEGEND = ['Paths', ['lb', 'server', 'bastion'].map((f) => [`tone-${f} link-ok link-targets`, FAMILY_LABEL[f]])]

// The VPC's label on its frame.
function VpcTag({ name, meta, children }) {
  return (
    <span className="map-vpc-tag">
      <span className="map-vpc-kind">
        <InfraIcon>{ICONS.vpc}</InfraIcon>
        VPC
      </span>
      {children ?? <strong>{name}</strong>}
      {meta && <span className="map-vpc-meta-line">{meta}</span>}
    </span>
  )
}

function ColumnTitle({ count, children }) {
  return (
    <span className="map-col-title">
      {children}
      {count > 0 && <span className="map-col-count">{count}</span>}
    </span>
  )
}

// A subnet's heading: a globe for a public one, a padlock for a private one.
function LaneHead({ label, isPublic, meta }) {
  return (
    <div className="map-lane-head">
      <span className="map-lane-icon" aria-hidden="true">
        <InfraIcon>{isPublic === true ? ICONS.internet : isPublic === false ? ICONS.lock : ICONS.vpc}</InfraIcon>
      </span>
      <strong>{label}</strong>
      {isPublic != null && <span className="map-lane-tag">{isPublic ? 'Public' : 'Private'}</span>}
      {meta && <span className="map-lane-meta">{meta}</span>}
    </div>
  )
}

// What nodes a focused one touches.
function neighbours(edges, focus) {
  const out = new Set(focus ? [focus] : [])
  if (!focus) return out
  for (const e of edges) {
    if (e.source === focus) out.add(e.target)
    if (e.target === focus) out.add(e.source)
  }
  return out
}

// --- the VPC map ---------------------------------------------------------------

const TRAFFIC = new Set(['internet', 'forwards', 'targets', 'registers', 'launches'])
const CHECKS = new Set(['probes', 'api'])

const EDGE_PHRASE = {
  internet: ['Reached from', 'Reachable from the internet'],
  forwards: ['Forwards from', 'Forwards to'],
  targets: ['Target of', 'Sends traffic to'],
  registers: ['Instances registered by', 'Registers its instances in'],
  launches: ['Launched by', 'Runs'],
  probes: ['Checked by', 'Checks'],
  api: ['Read through', 'Reads'],
  uses: ['Used by', 'Uses'],
}

function subLine(node) {
  switch (node.kind) {
    case 'instance':
      return [
        [node.address, STOPPED.has(node.aws_state) ? node.aws_state : null].filter(Boolean).join(' · '),
        node.public_ip && `public ${node.public_ip}`,
      ]
    case 'target_group':
      // Its targets, and on a line of its own the health check AWS runs.
      return [node.detail, node.health_check && `checks ${node.health_check}`]
    case 'auto_scaling_group':
      // "1 of 1 healthy", then "min 1, max 3".
      return node.detail?.split(' · ').slice(0, 2)
    case 'database':
    case 'load_balancer':
      return node.detail?.split(' · ').slice(0, 2).join(' · ')
    case 'watchly':
      return node.subnet ? 'in this VPC' : 'outside this VPC'
    case 'aws_api':
      return node.detail?.replace("Read with ", '').replace("'s credentials", ' credentials')
    case 'ip_target':
      return 'IP target'
    default:
      return null
  }
}

// Only what is monitored, and what it leads to: a load balancer's target
// groups, and their targets.
function monitoredOnly(nodes, edges) {
  const keep = new Set(nodes.filter((n) => n.resource_id || ['internet', 'watchly', 'aws_api'].includes(n.kind)).map((n) => n.id))
  for (let pass = 0; pass < 2; pass++) {
    for (const e of edges) {
      if (['forwards', 'targets', 'registers', 'launches'].includes(e.kind) && keep.has(e.source)) keep.add(e.target)
    }
  }
  return keep
}

// Target groups level with the load balancers that forward to them, each
// followed by the Auto Scaling groups that register in it, so the links run
// short and straight; then what nothing forwards to.
function routingOrder(visible, entry) {
  const routing = visible.nodes.filter((n) => n.kind === 'target_group' || n.kind === 'auto_scaling_group')
  routing.sort((a, b) => a.label.localeCompare(b.label))
  const from = (source, kind) => visible.edges.filter((e) => e.source === source && e.kind === kind).map((e) => e.target)
  const into = (target, kind) => visible.edges.filter((e) => e.target === target && e.kind === kind).map((e) => e.source)
  const order = []
  const add = (id) => !order.includes(id) && order.push(id)
  const addGroup = (tg) => {
    add(tg)
    into(tg, 'registers').forEach(add)
  }
  for (const lb of entry) from(lb.id, 'forwards').forEach(addGroup)
  for (const n of routing) {
    if (n.kind === 'target_group') addGroup(n.id)
    else {
      from(n.id, 'registers').forEach(addGroup)
      add(n.id)
    }
  }
  const byId = Object.fromEntries(routing.map((n) => [n.id, n]))
  return order.map((id) => byId[id]).filter(Boolean)
}

function WatchlyNote({ topology, vpcName }) {
  if (topology.watchly === 'in_vpc') {
    return <p className="small">Watchly runs inside {vpcName}: its probes reach private addresses as far as each security group allows.</p>
  }
  return (
    <p className="small">
      {topology.watchly === 'outside'
        ? `Watchly runs in another VPC. Probes to private addresses in ${vpcName} arrive only through peering or a transit gateway, with routes and security groups that allow Watchly's range.`
        : `Watchly is not running on EC2, so probes to private addresses in ${vpcName} cannot arrive.`}{' '}
      Checks over the internet, and what it reads from the AWS API (target and group health), work from anywhere.
    </p>
  )
}

function NodePanel({ node, topology, nodesById, resource, vpc, canAdd, onClose, onSelect }) {
  const edges = topology.edges.filter((e) => e.source === node.id || e.target === node.id)
  const rows = [
    ['AWS id', node.aws_id],
    ['Address', node.address],
    ['Public IP', node.public_ip],
    ['Subnet', topology.subnets.find((s) => s.id === node.subnet)?.label ?? node.subnet],
    ['AWS state', node.aws_state],
    ['Detail', node.detail],
    ['Health check', node.health_check],
  ].filter(([, v]) => v)
  return (
    <aside className="map-panel card" aria-label={`${node.label} details`}>
      <div className="map-panel-head">
        <span className={`map-node-icon mt-${toneOf(node)}`}>
          <NodeIcon kind={node.kind} />
        </span>
        <div className="map-panel-title">
          <strong>{node.label}</strong>
          <span className="muted small">{NODE_KIND_LABEL[node.kind] ?? node.kind}</span>
        </div>
        <button type="button" className="btn btn-sm" onClick={onClose} aria-label="Close details">
          ✕
        </button>
      </div>
      <div className="map-panel-badges">
        {node.state ? (
          <StateBadge state={node.state} />
        ) : RESOURCE_NODES.has(node.kind) ? (
          <span className="badge badge-unknown">Not monitored</span>
        ) : null}
        {node.aws_health && !node.state && <span className={`badge badge-${AWS_TONE[node.aws_health]}`}>AWS: {node.aws_health}</span>}
      </div>

      {node.kind === 'watchly' && <WatchlyNote topology={topology} vpcName={vpc.name} />}
      {rows.length > 0 && (
        <dl className="kv kv-compact">
          {rows.map(([k, v]) => (
            <div key={k}>
              <dt>{k}</dt>
              <dd>{k === 'AWS id' ? <code>{v}</code> : v}</dd>
            </div>
          ))}
        </dl>
      )}

      {resource && (
        <>
          <h3>Checks</h3>
          <ul className="map-panel-list">
            {resource.checks.map((c) => (
              <li key={c.id}>
                <span className={`check-dot health-${c.is_enabled ? c.health : 'paused'}`} />
                <span>
                  <strong>{c.name}</strong>
                  <span className="muted small"> {c.last_result?.summary ?? 'not checked yet'}</span>
                </span>
              </li>
            ))}
          </ul>
        </>
      )}

      {edges.length > 0 && (
        <>
          <h3>Connections</h3>
          <ul className="map-panel-list">
            {edges.map((e) => {
              const outgoing = e.source === node.id
              const other = nodesById[outgoing ? e.target : e.source]
              const [into, out] = EDGE_PHRASE[e.kind] ?? ['', '']
              const verb = e.kind === 'probes' && e.via ? `${outgoing ? out : into} ${e.via === 'internet' ? 'over the internet' : 'inside the VPC'}` : outgoing ? out : into
              return (
                <li key={`${e.source}>${e.target}>${e.kind}>${e.via ?? ''}`}>
                  <span className={`map-dot mt-${{ ok: 'healthy', failing: 'down', degraded: 'degraded', off: 'paused' }[e.state] ?? 'unknown'}`} />
                  <span>
                    <span className="muted small">{verb} </span>
                    {other ? (
                      <button type="button" className="link-button" onClick={() => onSelect(other.id)}>
                        {other.label}
                      </button>
                    ) : (
                      '?'
                    )}
                    {e.label && <span className="muted small"> · {e.label}</span>}
                    {e.detail && <span className={`small map-panel-detail${e.state === 'failing' ? ' text-down' : ''}`}>{e.detail}</span>}
                  </span>
                </li>
              )
            })}
          </ul>
        </>
      )}

      <div className="map-panel-actions">
        {node.resource_id ? (
          <Link to={`/infra/resources/${node.resource_id}`} className="btn btn-primary btn-sm">
            Open resource
          </Link>
        ) : (
          node.key &&
          canAdd && (
            <Link to={`/infra/new?project=${vpc.project.id}&vpc=${vpc.id}&pick=${encodeURIComponent(node.key)}`} className="btn btn-sm">
              Monitor this
            </Link>
          )
        )}
      </div>
    </aside>
  )
}

export function VpcMap({ topology, vpc, resources, canAdd, onRefresh, refreshing }) {
  const [hover, setHover] = useState(null)
  const [selected, setSelected] = useState(null)
  const [layers, setLayers] = useState({ traffic: true, checks: true, unmonitored: true })

  const nodesById = useMemo(() => Object.fromEntries(topology.nodes.map((n) => [n.id, n])), [topology])
  const resourcesById = useMemo(() => Object.fromEntries((resources ?? []).map((r) => [r.id, r])), [resources])

  const visible = useMemo(() => {
    const keep = layers.unmonitored ? null : monitoredOnly(topology.nodes, topology.edges)
    const nodes = topology.nodes
      .filter((n) => !keep || keep.has(n.id))
      .map((n) => ({
        ...n,
        monitored: RESOURCE_NODES.has(n.kind) ? Boolean(n.resource_id) : undefined,
      }))
    const ids = new Set(nodes.map((n) => n.id))
    const byId = Object.fromEntries(nodes.map((n) => [n.id, n]))
    const edges = topology.edges
      .filter(
        (e) =>
          ids.has(e.source) &&
          ids.has(e.target) &&
          ((layers.traffic && TRAFFIC.has(e.kind)) || (layers.checks && CHECKS.has(e.kind))),
      )
      .map((e) => ({ ...e, family: edgeFamily(e, (id) => nodeFamily(byId[id])) }))
    return { nodes, edges }
  }, [topology, layers])

  const { canvasRef, nodeRef, geometry } = useMeasure([visible, selected])
  const focus = hover ?? selected
  const related = neighbours(visible.edges, focus)
  const select = (id) => setSelected((current) => (current === id ? null : id))

  // Where each node goes.
  const inVpc = topology.watchly === 'in_vpc'
  const outside = visible.nodes.filter((n) => n.kind === 'internet' || n.kind === 'aws_api' || (n.kind === 'watchly' && !inVpc))
  outside.sort((a, b) => ['internet', 'watchly', 'aws_api'].indexOf(a.kind) - ['internet', 'watchly', 'aws_api'].indexOf(b.kind))
  const entry = visible.nodes.filter((n) => n.kind === 'load_balancer')
  const subnetIds = new Set(topology.subnets.map((s) => s.id))
  const placed = visible.nodes.filter(
    (n) => n.kind === 'instance' || n.kind === 'ip_target' || n.kind === 'database' || (n.kind === 'watchly' && inVpc),
  )
  const lanes = topology.subnets.map((s) => ({ ...s, nodes: placed.filter((n) => n.subnet === s.id) }))
  const elsewhere = placed.filter((n) => !subnetIds.has(n.subnet))
  if (elsewhere.length) lanes.push({ id: '_', label: topology.subnets.length ? 'Subnet unknown' : 'Instances', public: null, nodes: elsewhere })
  const byName = (a, b) => (a.kind === 'watchly' ? -1 : b.kind === 'watchly' ? 1 : 0) || Boolean(b.resource_id) - Boolean(a.resource_id) || a.label.localeCompare(b.label)
  for (const lane of lanes) lane.nodes.sort(byName)
  entry.sort(byName)
  const routing = routingOrder(visible, entry)
  const shownLanes = lanes.filter((l) => l.nodes.length || layers.unmonitored)
  // Public subnets, then private ones, each band under its own heading.
  const bands = [
    ['public', 'Public subnets', 'Route to an internet gateway', shownLanes.filter((l) => l.public === true)],
    ['private', 'Private subnets', 'No route in from the internet', shownLanes.filter((l) => l.public === false)],
    ['unknown', 'Other subnets', 'Routing not read', shownLanes.filter((l) => l.public == null)],
  ].filter(([, , , list]) => list.length)

  const node = (n) => (
    <MapNode
      key={n.id}
      node={n}
      nodeRef={nodeRef}
      sub={subLine(n)}
      tone={toneOf(n)}
      family={RESOURCE_NODES.has(n.kind) || n.kind === 'target_group' || n.kind === 'ip_target' ? nodeFamily(n) : null}
      focus={focus}
      related={related.has(n.id)}
      selected={selected === n.id}
      onSelect={select}
      onHover={setHover}
    />
  )
  const chosen = selected ? nodesById[selected] : null
  const failingProbes = topology.edges.filter((e) => e.kind === 'probes' && e.state === 'failing').length

  return (
    <div className="map">
      <div className="map-toolbar">
        <div className="map-toggles" role="group" aria-label="Show on the map">
          {[
            ['traffic', 'Traffic paths'],
            ['checks', "Watchly's checks"],
            ['unmonitored', 'Unmonitored resources'],
          ].map(([key, label]) => (
            <label key={key} className={`map-toggle${layers[key] ? ' is-on' : ''}`}>
              <input type="checkbox" checked={layers[key]} onChange={() => setLayers({ ...layers, [key]: !layers[key] })} />
              {label}
            </label>
          ))}
        </div>
        <span className="muted small">
          Read from AWS {timeAgo(topology.read_at)}
          {onRefresh && (
            <>
              {' · '}
              <button type="button" className="link-button" onClick={onRefresh} disabled={refreshing}>
                {refreshing ? 'reading…' : 'read again'}
              </button>
            </>
          )}
        </span>
      </div>

      {topology.aws_error && (
        <div className="banner banner-error small">
          AWS listed nothing, so only monitored resources are drawn: {topology.aws_error}
        </div>
      )}
      {topology.skipped.length > 0 && (
        <div className="banner banner-info small">
          Drawn without {topology.skipped.map((s) => `${s.service}:${s.call}`).join(', ')}, which AWS refused (
          {topology.skipped[0].reason}).
        </div>
      )}
      {failingProbes > 0 && topology.watchly !== 'in_vpc' && (
        <div className="banner banner-info small">
          <WatchlyNote topology={topology} vpcName={vpc.name} />
        </div>
      )}

      <div className={`map-shell${chosen ? ' has-panel' : ''}`}>
        <div className="map-scroll" onClick={(e) => e.target === e.currentTarget && setSelected(null)}>
          <div className="map-canvas map-canvas-vpc" ref={canvasRef}>
            <Links edges={visible.edges} geometry={geometry} focus={focus} />
            <div className="map-col map-col-outside">{outside.map(node)}</div>
            <section className="map-vpc" aria-label={`VPC ${vpc.name}`}>
              <VpcTag name={vpc.name} meta={`${vpc.cidrs.join(', ')} · ${vpc.region}`} />
              <div className="map-vpc-body">
                <div className="map-col">
                  <ColumnTitle count={entry.length}>Load balancers</ColumnTitle>
                  {entry.length ? entry.map(node) : <span className="map-col-empty">none</span>}
                </div>
                <div className="map-col">
                  <ColumnTitle count={routing.length}>Target &amp; scaling groups</ColumnTitle>
                  {routing.length ? routing.map(node) : <span className="map-col-empty">none</span>}
                </div>
                <div className="map-lanes">
                  {bands.map(([key, title, note, list]) => (
                    <div key={key} className={`map-band is-${key}`}>
                      <div className="map-band-head">
                        <strong>{title}</strong>
                        <span>{note}</span>
                      </div>
                      {list.map((lane) => (
                        <div key={lane.id} className={`map-lane is-${key}`}>
                          <LaneHead label={lane.label} isPublic={lane.public} meta={[lane.cidr, lane.az].filter(Boolean).join(' · ')} />
                          <div className="map-lane-body">
                            {lane.nodes.length ? lane.nodes.map(node) : <span className="map-col-empty">nothing here</span>}
                          </div>
                        </div>
                      ))}
                    </div>
                  ))}
                  {!shownLanes.length && <span className="map-col-empty">No instances</span>}
                </div>
              </div>
            </section>
          </div>
        </div>
        {chosen && (
          <NodePanel
            node={chosen}
            topology={topology}
            nodesById={nodesById}
            resource={resourcesById[chosen.resource_id]}
            vpc={vpc}
            canAdd={canAdd}
            onClose={() => setSelected(null)}
            onSelect={setSelected}
          />
        )}
      </div>

      <Legend
        groups={[
          FAMILY_LEGEND,
          [
            'Lines',
            [
              ['tone-neutral link-ok link-targets', 'Traffic'],
              ['tone-neutral link-ok link-internet', 'From the internet'],
              ['tone-neutral link-ok link-registers', 'Registers instances'],
              ['tone-neutral link-ok link-probes', 'Watchly probe'],
              ['tone-neutral link-ok link-probes via-internet', 'Probe over the internet'],
              ['tone-neutral link-ok link-api', 'AWS API read'],
            ],
          ],
          [
            'State',
            [
              ['mt-healthy', 'Healthy'],
              ['mt-degraded', 'Degraded'],
              ['mt-down', 'Down'],
              ['mt-paused', 'Paused or stopped'],
              ['mt-none', 'Not monitored'],
              ['tone-failing link-failing link-probes', 'Failing'],
              ['tone-lb link-degraded link-targets', 'Degraded path'],
            ],
          ],
        ]}
      />
    </div>
  )
}

// --- the infrastructure map -------------------------------------------------------

const REACH_EDGE = { reachable: 'ok', unreachable: 'failing', untested: 'unknown' }
const STATE_EDGE = {
  healthy: 'ok',
  degraded: 'degraded',
  down: 'failing',
  missing: 'failing',
  maintenance: 'off',
  paused: 'off',
  unknown: 'unknown',
}
const NODE_OF_KIND = { server: 'instance', load_balancer: 'load_balancer', auto_scaling_group: 'auto_scaling_group', database: 'database' }
const VPC_TONE = { unreachable: 'down', untested: 'unknown' }

const resourceNode = (r) => `r:${r.id}`
const REACH_RANK = ['unreachable', 'untested', 'reachable']

// One frame per AWS VPC: each project that is granted it holds its own record
// of it, maybe through its own account record, all drawn as the one network
// they are. VPC ids are unique across accounts.
function networks(vpcs) {
  const byKey = new Map()
  for (const v of vpcs) {
    const key = v.aws_vpc_id ? `${v.region}:${v.aws_vpc_id}` : `id:${v.id}`
    const net = byKey.get(key)
    if (!net) {
      byKey.set(key, { ...v, key, records: [v], counts: { ...v.counts } })
      continue
    }
    net.records.push(v)
    for (const k of Object.keys(net.counts)) net.counts[k] += v.counts[k] ?? 0
    if (REACH_RANK.indexOf(v.health) < REACH_RANK.indexOf(net.health)) net.health = v.health
    net.is_watchly_vpc ||= v.is_watchly_vpc
  }
  return [...byKey.values()]
}

// Each VPC's resources in columns and lanes, and the links between them, from
// the AWS detail each resource carries:
// - the internet reaches what has a public address;
// - a load balancer sends traffic to the Auto Scaling groups that share its
//   target groups, and a group runs the servers that name it;
// - a service's groups or servers use its databases (see groupServices);
// - Watchly reaches each VPC, or, with its checks shown, each resource.
// Where a VPC's topology was read from AWS, a load balancer also sends traffic
// to the monitored servers in its target groups, through an Auto Scaling group
// that is not monitored too, and its subnets say which are public.
function infraGraph(vpcs, resources, checks, topologies) {
  const edges = []
  const frames = networks(vpcs).map((vpc) => {
    const ids = new Set(vpc.records.map((v) => v.id))
    const items = resources.filter((r) => ids.has(r.vpc.id))
    const maps = topologies.filter((t) => t && ids.has(t.vpc_id))
    const of = (kind) => items.filter((r) => r.kind === kind).sort((a, b) => a.name.localeCompare(b.name))
    const [lbs, groups, servers, databases] = ['load_balancer', 'auto_scaling_group', 'server', 'database'].map(of)
    const state = (r) => STATE_EDGE[r.state] ?? 'unknown'

    // Internet-facing load balancers on top, and each group level with the
    // first one that sends it traffic, so the links run straight.
    lbs.sort((a, b) => isPublic(b) - isPublic(a))
    const fronted = new Map()
    lbs.forEach((lb, rank) => {
      const arns = new Set((lb.aws_detail?.target_groups ?? []).map((tg) => tg.arn))
      for (const g of groups) {
        if ((g.aws_detail?.target_groups ?? []).some((tg) => arns.has(tg.arn))) {
          edges.push({ source: resourceNode(lb), target: resourceNode(g), kind: 'targets', state: state(g) })
          if (!fronted.has(g.id)) fronted.set(g.id, rank)
        }
      }
    })
    groups.sort((a, b) => (fronted.get(a.id) ?? lbs.length) - (fronted.get(b.id) ?? lbs.length))
    const publicSubnets = new Map(maps.flatMap((t) => t.subnets.filter((sn) => sn.public != null).map((sn) => [sn.label, sn.public])))
    const groupsByName = new Map(groups.map((g) => [g.aws_id, g]))
    const inGroup = (r) => groupsByName.has(r.aws_detail?.auto_scaling_group)
    for (const r of servers.filter(inGroup)) {
      const g = groupsByName.get(r.aws_detail.auto_scaling_group)
      edges.push({ source: resourceNode(g), target: resourceNode(r), kind: 'launches', state: state(r) })
    }
    const linked = new Set(edges.map((e) => `${e.source}>${e.target}`))
    const byId = new Map(items.map((r) => [r.id, r]))
    for (const t of maps) {
      const out = (id, kind) => t.edges.filter((e) => e.source === id && e.kind === kind).map((e) => e.target)
      const nodes = new Map(t.nodes.map((n) => [n.id, n]))
      for (const lbNode of t.nodes.filter((n) => n.kind === 'load_balancer' && byId.has(n.resource_id))) {
        const lb = byId.get(lbNode.resource_id)
        for (const target of out(lbNode.id, 'forwards').flatMap((tg) => out(tg, 'targets'))) {
          const r = byId.get(nodes.get(target)?.resource_id)
          const key = r && `${resourceNode(lb)}>${resourceNode(r)}`
          if (!r || r.kind !== 'server' || inGroup(r) || linked.has(key)) continue
          edges.push({ source: resourceNode(lb), target: resourceNode(r), kind: 'targets', state: state(r) })
          linked.add(key)
        }
      }
    }
    for (const r of items) {
      if (isPublic(r) && !fronted.has(r.id)) edges.push({ source: 'internet', target: resourceNode(r), kind: 'internet', state: 'ok' })
    }
    for (const service of groupServices(items)) {
      if (service.name === null) continue
      const parts = service.resources
      const users =
        [
          parts.filter((r) => r.kind === 'auto_scaling_group'),
          parts.filter((r) => r.kind === 'server' && !inGroup(r)),
          parts.filter((r) => r.kind === 'load_balancer'),
        ].find((list) => list.length) ?? []
      for (const db of parts.filter((r) => r.kind === 'database')) {
        for (const u of users) edges.push({ source: resourceNode(u), target: resourceNode(db), kind: 'uses', state: state(db) })
      }
    }
    if (checks) {
      for (const r of items) {
        edges.push({
          source: 'watchly',
          target: resourceNode(r),
          kind: 'probes',
          state: r.is_enabled ? state(r) : 'off',
          via: isPublic(r) ? 'internet' : 'vpc',
        })
      }
    } else {
      edges.push({ source: 'watchly', target: `vpc:${vpc.key}`, kind: 'probes', state: REACH_EDGE[vpc.health] ?? 'unknown' })
    }

    // Servers and databases in lanes by subnet, the public ones first.
    const lanes = new Map()
    for (const r of [...servers, ...databases]) {
      const name = r.aws_detail?.subnet ?? null
      const lane = lanes.get(name) ?? { id: name ?? '_', label: name ?? 'Subnet unknown', known: Boolean(name), public: false, resources: [] }
      lane.public = publicSubnets.get(name) ?? (lane.public || isPublic(r))
      lane.resources.push(r)
      lanes.set(name, lane)
    }
    const ordered = [...lanes.values()].sort((a, b) => b.known - a.known || b.public - a.public || a.label.localeCompare(b.label))
    return { vpc, items, lbs, groups, lanes: ordered }
  })
  const families = Object.fromEntries(resources.map((r) => [resourceNode(r), resourceFamily(r)]))
  return { frames, edges: edges.map((e) => ({ ...e, family: edgeFamily(e, (id) => families[id]) })) }
}

function resourceSub(r) {
  const d = r.aws_detail ?? {}
  switch (r.kind) {
    case 'server':
      return [r.address ?? d.private_ip, d.public_ip && 'public IP', d.instance_type].filter(Boolean).join(' · ')
    case 'auto_scaling_group': {
      const inService = (d.instances ?? []).filter((i) => i.lifecycle_state === 'InService').length
      return `${inService} in service · ${d.min_size}–${d.max_size}`
    }
    case 'load_balancer': {
      const listeners = (d.listeners ?? []).map((l) => `${l.protocol}:${l.port}`).join(', ')
      return [exposureLine(r), listeners].filter(Boolean).join(' · ')
    }
    default:
      return exposureLine(r)
  }
}

const EDGE_TONE = { ok: 'healthy', failing: 'down', degraded: 'degraded', off: 'paused' }

function ResourcePanel({ resource, edges, labels, onClose, onSelect }) {
  const id = resourceNode(resource)
  const d = resource.aws_detail ?? {}
  const trouble = resourceTrouble(resource)
  const started = resource.down_since ?? resource.degraded_since ?? resource.missing_since
  const instances = resource.kind === 'auto_scaling_group' ? groupInstances(resource) : []
  const links = edges.filter((e) => e.source === id || e.target === id)
  const rows = [
    ['AWS id', resource.aws_id],
    ['Address', resource.address],
    ['Public IP', d.public_ip],
    ['Subnet', d.subnet],
    ['Detail', exposureLine(resource)],
    ['Project', resource.project?.name],
  ].filter(([, v]) => v)
  return (
    <aside className="map-panel card" aria-label={`${resource.name} details`}>
      <div className="map-panel-head">
        <span className={`map-node-icon mt-${STATE_TONE[resource.state] ?? 'unknown'}`}>
          <KindIcon kind={resource.kind} />
        </span>
        <div className="map-panel-title">
          <strong>{resource.name}</strong>
          <span className="muted small">
            {kindInfo(resource.kind).label} · {resource.vpc.name}
          </span>
        </div>
        <button type="button" className="btn btn-sm" onClick={onClose} aria-label="Close details">
          ✕
        </button>
      </div>
      <div className="map-panel-badges">
        <StateBadge state={resource.state} />
        <EnvironmentBadge environment={resource.environment} />
      </div>
      {trouble && resource.state !== 'healthy' && (
        <p className={`small map-panel-trouble ${resource.state === 'degraded' ? 'text-pending' : 'text-down'}`}>
          {trouble}
          {started && <span className="muted"> · for {since(started)}</span>}
        </p>
      )}

      <dl className="kv kv-compact">
        {rows.map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{k === 'AWS id' ? <code>{v}</code> : v}</dd>
          </div>
        ))}
      </dl>

      {instances.length > 0 && (
        <>
          <h3>Instances</h3>
          <ul className="map-panel-list">
            {instances.map((i) => (
              <li key={i.id}>
                <span className={`map-dot mt-${STATE_TONE[i.state] ?? 'unknown'}`} />
                <span>
                  <code className="small">{i.id}</code>
                  {i.note && <span className="small map-panel-detail">{i.note}</span>}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}

      {resource.checks.length > 0 && (
        <>
          <h3>Checks</h3>
          <ul className="map-panel-list">
            {resource.checks.map((c) => (
              <li key={c.id}>
                <span className={`check-dot health-${c.is_enabled ? c.health : 'paused'}`} />
                <span>
                  <strong>{c.name}</strong>
                  <span className="muted small"> {c.last_result?.summary ?? 'not checked yet'}</span>
                </span>
              </li>
            ))}
          </ul>
        </>
      )}

      {links.length > 0 && (
        <>
          <h3>Connections</h3>
          <ul className="map-panel-list">
            {links.map((e) => {
              const outgoing = e.source === id
              const other = outgoing ? e.target : e.source
              const [into, out] = EDGE_PHRASE[e.kind] ?? ['', '']
              return (
                <li key={`${e.source}>${e.target}>${e.kind}`}>
                  <span className={`map-dot mt-${EDGE_TONE[e.state] ?? 'unknown'}`} />
                  <span>
                    <span className="muted small">{outgoing ? out : into} </span>
                    {other.startsWith('r:') ? (
                      <button type="button" className="link-button" onClick={() => onSelect(other)}>
                        {labels[other]}
                      </button>
                    ) : (
                      labels[other]
                    )}
                  </span>
                </li>
              )
            })}
          </ul>
        </>
      )}

      <div className="map-panel-actions">
        <Link to={`/infra/resources/${resource.id}`} className="btn btn-primary btn-sm">
          Open resource
        </Link>
        <Link to={`/infra/vpcs/${resource.vpc.id}`} className="btn btn-sm">
          VPC map
        </Link>
      </div>
    </aside>
  )
}

function VpcFrame({ frame, node, nodeRef }) {
  const { vpc, items, lbs, groups, lanes } = frame
  const c = vpc.counts
  const columns = [
    lbs.length > 0 && ['Load balancers', lbs, '200px'],
    groups.length > 0 && ['Auto Scaling groups', groups, '220px'],
  ].filter(Boolean)
  const template = [...columns.map((col) => col[2]), lanes.length > 0 && 'minmax(260px, 1fr)'].filter(Boolean).join(' ')
  const tone = vpc.health === 'unreachable' ? 'down' : c.total ? STATE_TONE[groupState(c)] : (VPC_TONE[vpc.health] ?? 'healthy')
  const health =
    vpc.health === 'unreachable' ? 'Unreachable' : c.total ? groupSummary(c) : vpc.health === 'untested' ? 'Not tested yet' : 'Reachable'
  const shared = vpc.records.length > 1
  return (
    <section ref={nodeRef(`vpc:${vpc.key}`)} className={`map-vpc map-vpc-frame mt-${tone}`} aria-label={`VPC ${vpc.name}`}>
      <VpcTag meta={[vpc.cidrs.join(', '), vpc.region].join(' · ')}>
        {shared ? (
          <strong>{vpc.name}</strong>
        ) : (
          <Link to={`/infra/vpcs/${vpc.id}`} className="strong-link" title={`Open the network map of ${vpc.name}`}>
            {vpc.name}
          </Link>
        )}
      </VpcTag>
      <div className="map-vpc-meta small">
        <span className="muted">{[vpc.account?.name, vpc.is_watchly_vpc && 'Watchly runs here'].filter(Boolean).join(' · ')}</span>
        {shared && (
          <span className="map-vpc-projects">
            <span className="muted">Shared by</span>
            {vpc.records.map((v) => (
              <Link key={v.id} to={`/infra/vpcs/${v.id}`} className="map-vpc-project" title={`${vpc.name} as ${v.project?.name ?? 'its project'} sees it`}>
                {v.project?.name ?? v.name}
              </Link>
            ))}
          </span>
        )}
      </div>
      <span className="map-vpc-health small">
        <span className={`map-dot${tone === 'down' ? ' is-alarm' : ''}`} />
        {health}
      </span>
      {items.length ? (
        <div className="map-vpc-body" style={{ gridTemplateColumns: template }}>
          {columns.map(([title, list]) => (
            <div key={title} className="map-col">
              <ColumnTitle count={list.length}>{title}</ColumnTitle>
              {list.map(node)}
            </div>
          ))}
          {lanes.length > 0 && (
            <div className="map-lanes">
              {lanes.map((lane) => (
                <div key={lane.id} className={`map-lane ${lane.known ? (lane.public ? 'is-public' : 'is-private') : 'is-unknown'}`}>
                  <LaneHead label={lane.label} isPublic={lane.known ? lane.public : null} />
                  <div className="map-lane-body">{lane.resources.map(node)}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : (
        <p className="map-col-empty">Nothing watched in this VPC yet.</p>
      )}
    </section>
  )
}

const NO_TOPOLOGIES = []

// `match` keeps the resources a search or filter picks; the rest fade, and
// `topologies` may hold a null for a VPC AWS could not be read for.
export function InfraMap({ vpcs, resources, topologies = NO_TOPOLOGIES, match, settingsTo }) {
  const [hover, setHover] = useState(null)
  const [selected, setSelected] = useState(null)
  const [checks, setChecks] = useState(false)

  const { frames, edges } = useMemo(
    () => infraGraph(vpcs, resources, checks, topologies),
    [vpcs, resources, checks, topologies],
  )
  const byNode = useMemo(() => Object.fromEntries(resources.map((r) => [resourceNode(r), r])), [resources])
  const labels = useMemo(
    () => ({
      internet: 'the internet',
      watchly: 'Watchly',
      ...Object.fromEntries(resources.map((r) => [resourceNode(r), r.name])),
    }),
    [resources],
  )

  const { canvasRef, nodeRef, geometry } = useMeasure([frames, selected])
  const focus = hover ?? selected
  const related = neighbours(edges, focus)
  const chosen = byNode[selected]
  const select = (id) => setSelected((current) => (current === id ? null : id))
  const home = vpcs.find((v) => v.is_watchly_vpc)
  const reachable = edges.some((e) => e.kind === 'internet')

  const node = (r) => {
    const id = resourceNode(r)
    const instances = r.kind === 'auto_scaling_group' ? groupInstances(r) : []
    return (
      <MapNode
        key={id}
        node={{ id, kind: NODE_OF_KIND[r.kind], label: r.name, title: resourceTrouble(r) ?? stateLabel(r.state) }}
        nodeRef={nodeRef}
        sub={resourceSub(r)}
        tone={STATE_TONE[r.state] ?? 'unknown'}
        family={resourceFamily(r)}
        focus={focus}
        related={related.has(id)}
        selected={selected === id}
        faded={match && !match(r)}
        onSelect={select}
        onHover={setHover}
      >
        {instances.length > 0 && (
          <span className="map-chips" aria-label={`${instances.length} instances`}>
            {instances.map((i) => (
              <i key={i.id} className={`map-chip mt-${STATE_TONE[i.state] ?? 'unknown'}`} title={i.note ? `${i.id}: ${i.note}` : i.id} />
            ))}
          </span>
        )}
      </MapNode>
    )
  }
  const outside = (n, sub, tone, to) => (
    <MapNode node={n} nodeRef={nodeRef} sub={sub} tone={tone} focus={focus} related={related.has(n.id)} onHover={setHover} to={to} />
  )

  return (
    <div className="map">
      <div className="map-toolbar">
        <div className="map-toggles" role="group" aria-label="Show on the map">
          <label className={`map-toggle${checks ? ' is-on' : ''}`}>
            <input type="checkbox" checked={checks} onChange={() => setChecks(!checks)} />
            Watchly&apos;s check of each resource
          </label>
        </div>
        <span className="muted small">Hover to trace a path · click a resource for its details</span>
      </div>

      <div className={`map-shell${chosen ? ' has-panel' : ''}`}>
        <div className="map-scroll" onClick={(e) => e.target === e.currentTarget && setSelected(null)}>
          <div className="map-canvas map-canvas-infra" ref={canvasRef}>
            <Links edges={edges} geometry={geometry} focus={focus} />
            <div className="map-col map-col-outside">
              {reachable && outside({ id: 'internet', kind: 'internet', label: 'Internet' }, 'public addresses', 'none')}
              {outside(
                { id: 'watchly', kind: 'watchly', label: 'Watchly', title: 'This monitoring server' },
                home ? `in ${home.name}` : `${frames.length} VPC${frames.length === 1 ? '' : 's'}`,
                'primary',
                settingsTo,
              )}
            </div>
            <div className="map-col map-col-frames">
              {frames.map((frame) => (
                <VpcFrame key={frame.vpc.key} frame={frame} node={node} nodeRef={nodeRef} />
              ))}
            </div>
          </div>
        </div>
        {chosen && (
          <ResourcePanel resource={chosen} edges={edges} labels={labels} onClose={() => setSelected(null)} onSelect={setSelected} />
        )}
      </div>

      <Legend
        groups={[
          FAMILY_LEGEND,
          [
            'Lines',
            [
              ['tone-neutral link-ok link-targets', 'Traffic'],
              ['tone-neutral link-ok link-internet', 'From the internet'],
              ['tone-db link-ok link-uses', 'Uses a database'],
              ['tone-neutral link-ok link-probes', checks ? 'Watchly check' : 'Watchly reaches the VPC'],
            ],
          ],
          [
            'State',
            [
              ['mt-healthy', 'Healthy'],
              ['mt-degraded', 'Degraded'],
              ['mt-down', 'Down'],
              ['mt-paused', 'Paused'],
              ['tone-failing link-failing link-probes', checks ? 'Failing' : 'Unreachable'],
            ],
          ],
        ]}
      />
    </div>
  )
}
