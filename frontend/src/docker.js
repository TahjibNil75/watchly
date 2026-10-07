import { useEffect, useState } from 'react'
import { api } from './api.js'

// The agent's image, published by github.com/TahjibNil75/watchly-docker-agent.
export const AGENT_IMAGE = 'ghcr.io/tahjibnil75/watchly-docker-agent:latest'
export const AGENT_REPO = 'https://github.com/TahjibNil75/watchly-docker-agent'

// Whether the API has Docker monitoring on; asked once per page load.
let enabledOnce = null
export function useDockerEnabled() {
  const [enabled, setEnabled] = useState(null)
  useEffect(() => {
    enabledOnce ??= api
      .health()
      .then((health) => health.docker === 'on')
      .catch(() => false)
    let live = true
    enabledOnce.then((value) => live && setEnabled(value))
    return () => {
      live = false
    }
  }, [])
  return enabled
}

// A container's condition as the API decides it, with how it reads and which
// existing status colour it borrows.
export const CONDITIONS = {
  running: { label: 'running', badge: 'up' },
  unhealthy: { label: 'unhealthy', badge: 'degraded' },
  down: { label: 'down', badge: 'down' },
  restarting: { label: 'restarting', badge: 'down' },
  paused: { label: 'paused', badge: 'paused' },
  stopped: { label: 'stopped', badge: 'paused' },
  unknown: { label: 'unknown', badge: 'unknown' },
  removed: { label: 'removed', badge: 'missing' },
}

export const HOST_STATUSES = {
  pending: { label: 'waiting for agent', badge: 'unknown' },
  online: { label: 'online', badge: 'up' },
  offline: { label: 'offline', badge: 'down' },
  docker_down: { label: 'Docker unreachable', badge: 'down' },
}

// The cards over the container list: everything, then one per condition group.
export const FILTERS = [
  { key: 'all', label: 'All containers', className: 'stat-all' },
  { key: 'running', label: 'Running', className: 'stat-up', note: 'Up, healthy or without a healthcheck' },
  { key: 'unhealthy', label: 'Unhealthy', className: 'stat-degraded', note: 'Running, but failing its healthcheck' },
  { key: 'down', label: 'Down', className: 'stat-down', note: 'Stopped after running, or restarting' },
  { key: 'stopped', label: 'Stopped', className: 'stat-paused', note: 'Never seen running, e.g. a finished job' },
]

export function matchesFilter(container, filter) {
  switch (filter) {
    case 'running':
      return ['running', 'paused', 'unhealthy'].includes(container.condition)
    case 'unhealthy':
      return container.condition === 'unhealthy'
    case 'down':
      return ['down', 'restarting'].includes(container.condition)
    case 'stopped':
      return container.condition === 'stopped'
    default:
      return true
  }
}

// What each open problem is called on the page.
export const PROBLEM_LABELS = {
  unhealthy: 'Unhealthy',
  restart_loop: 'Restart loop',
  high_cpu: 'High CPU',
  high_memory: 'High memory',
}

// 536870912 -> '512 MiB'. Docker reports memory in binary units.
export function memory(n) {
  if (n == null) return '—'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let value = n
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit++
  }
  return `${value >= 100 || unit < 2 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`
}

// Bytes per second, as network graphs usually read.
export function rate(bps) {
  if (bps == null) return '—'
  return `${memory(bps)}/s`
}

// The agent's cpu_pct is 100 per core; shown as a share of the whole host
// when its core count is known, which is what the CPU alert measures.
export function cpuShare(cpuPct, cpus) {
  if (cpuPct == null) return null
  return cpus ? cpuPct / cpus : cpuPct
}

export function pct(value, digits = 0) {
  return value == null ? '—' : `${value.toFixed(digits)}%`
}

// Where the agent should send its pushes: this page's own origin, which serves
// the API under /api. A dev server on localhost is not reachable from a remote
// host, but the snippet is still the right shape.
export function watchlyUrl() {
  return window.location.origin
}

export function dockerRunSnippet(token) {
  return [
    'docker run -d --name watchly-agent --restart unless-stopped \\',
    `  -e WATCHLY_URL=${watchlyUrl()} \\`,
    `  -e WATCHLY_TOKEN=${token} \\`,
    ...(watchlyUrl().startsWith('http://') ? ['  -e WATCHLY_INSECURE_HTTP=true \\'] : []),
    '  --group-add "$(stat -c %g /var/run/docker.sock)" \\',
    '  -v /var/run/docker.sock:/var/run/docker.sock:ro \\',
    '  --read-only --cap-drop ALL --security-opt no-new-privileges:true \\',
    '  --cpus 0.1 --memory 64m \\',
    `  ${AGENT_IMAGE}`,
  ].join('\n')
}

export function composeSnippet(token) {
  const insecure = watchlyUrl().startsWith('http://') ? ['      WATCHLY_INSECURE_HTTP: "true"'] : []
  return [
    'services:',
    '  watchly-agent:',
    `    image: ${AGENT_IMAGE}`,
    '    container_name: watchly-agent',
    '    restart: unless-stopped',
    '    environment:',
    `      WATCHLY_URL: ${watchlyUrl()}`,
    `      WATCHLY_TOKEN: ${token}`,
    ...insecure,
    '    group_add:',
    '      - "${DOCKER_GID}"  # stat -c %g /var/run/docker.sock',
    '    volumes:',
    '      - /var/run/docker.sock:/var/run/docker.sock:ro',
    '    read_only: true',
    '    cap_drop: [ALL]',
    '    security_opt: ["no-new-privileges:true"]',
    '    cpus: 0.1',
    '    mem_limit: 64m',
  ].join('\n')
}
