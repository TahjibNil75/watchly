import { useEffect, useState } from 'react'

// The night-shift monitor's furniture, shared by the sign-in page and the
// landing page's hero: a heartbeat sweeping a dark grid, and a readout in each
// corner. Both take their colour from the mood of the .night / .night-hero
// they sit in (index.css). The readouts play out a site being watched; they
// aren't real data.

const READOUTS = {
  up: { status: 'Up', response: '142 ms', alerts: 'None' },
  slow: { status: 'Slow', response: '2.4 s', alerts: 'None' },
  down: { status: 'Down', response: 'Timeout', alerts: 'Sent' },
  maint: { status: 'Maintenance', response: 'Paused', alerts: 'Muted' },
}

// A heartbeat, a lazy wave, a flatline, and a dashed line for paused checks,
// across a 1000x600 box stretched over its parent.
const BEAT = 'h160l20-50 24 140 22-120 14 30'
const TRACES = {
  up: `M0 330h110l20-50 24 140 22-120 14 30${BEAT}${BEAT}${BEAT}h96`,
  slow: `M0 330q62.5-70 125 0${'t125 0'.repeat(7)}`,
  down: 'M0 330h1000',
  maint: 'M0 330h1000',
}

function UtcClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(timer)
  }, [])
  return now.toISOString().slice(11, 19)
}

// Every mood's trace is drawn; the current one's shows.
export function NightTraces() {
  return (
    <svg className="night-ecg" viewBox="0 0 1000 600" preserveAspectRatio="none" aria-hidden="true">
      {Object.entries(TRACES).map(([name, d]) => (
        <path key={name} className={`night-trace night-trace-${name}`} pathLength="1" d={d} />
      ))}
    </svg>
  )
}

export function NightReadouts({ mood }) {
  const readout = READOUTS[mood]
  return (
    <div className="night-readouts" aria-hidden="true">
      <div className="night-read is-tl">
        Status<b>{readout.status}</b>
      </div>
      <div className="night-read is-tr">
        Response<b>{readout.response}</b>
      </div>
      <div className="night-read is-bl">
        UTC<b>
          <UtcClock />
        </b>
      </div>
      <div className="night-read is-br">
        Alerts<b>{readout.alerts}</b>
      </div>
    </div>
  )
}
