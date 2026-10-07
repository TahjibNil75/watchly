import { api } from './api.js'
import { useApi } from './useApi.js'

const POLL_MS = 30000

// What the sidebar badges and the overview tiles show: each monitor's total
// and how many of them need a look. `infra` and `docker` are whether the API
// has that monitor on; an off monitor reports nothing. A monitor whose call
// fails (or hasn't answered yet) is null, so its badge stays blank.
export function useNavCounts({ infra, docker }) {
  const sites = useApi(() => api.websiteSummary({}), [], { pollMs: POLL_MS })
  const resources = useApi(() => (infra ? api.resourceSummary({}) : null), [infra], { pollMs: POLL_MS })
  const hosts = useApi(() => (docker ? api.listDockerHosts() : null), [docker], { pollMs: POLL_MS })

  const w = sites.data
  const r = resources.data
  const h = hosts.data?.items

  return {
    websites: w && { ...w, problems: w.down },
    infra: r && { ...r, problems: r.down + r.degraded },
    docker: h && dockerTotals(h),
  }
}

function dockerTotals(hosts) {
  const sum = (key) => hosts.reduce((n, host) => n + (host.counts?.[key] ?? 0), 0)
  const down = sum('down')
  const unhealthy = sum('unhealthy')
  return {
    hosts: hosts.length,
    total: sum('total'),
    running: sum('running'),
    stopped: sum('stopped'),
    down,
    unhealthy,
    problems: down + unhealthy,
  }
}
