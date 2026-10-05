// The landing page's AWS parts (Landing.jsx): the hero's row of what Watchly
// reads from AWS and its corner readouts, a VPC drawn as the network map draws it, the one alert a lost
// VPC sends, and a deployment that pages nobody. All made-up examples, worded
// as app/monitoring/infra/aws/events.py words the real alerts. They wear the
// landing page's own palette: --glow is the chapter's mood.

// 24x24 stroke icons, drawn as Landing.jsx's are.
export const AWS_ICONS = {
  server: (
    <>
      <rect x="5" y="5" width="14" height="14" rx="2" />
      <path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3" />
    </>
  ),
  asg: (
    <>
      <rect x="3" y="4" width="6" height="6" rx="1" />
      <rect x="15" y="4" width="6" height="6" rx="1" />
      <rect x="9" y="14" width="6" height="6" rx="1" />
      <path d="M6 10v2h12v-2M12 12v2" />
    </>
  ),
  lb: (
    <>
      <circle cx="12" cy="5" r="2.5" />
      <circle cx="5" cy="19" r="2.5" />
      <circle cx="12" cy="19" r="2.5" />
      <circle cx="19" cy="19" r="2.5" />
      <path d="M12 7.5v9M12 10l-7 6.5M12 10l7 6.5" />
    </>
  ),
  db: (
    <>
      <ellipse cx="12" cy="5" rx="8" ry="3" />
      <path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" />
    </>
  ),
  deploy: <path d="M12 3v12M7 10l5 5 5-5M4 21h16" />,
  shield: <path d="M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6l-8-3zM8.5 12l2.5 2.5 4.5-5" />,
}

function Glyph({ name }) {
  return (
    <svg
      className="channel-logo"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {AWS_ICONS[name]}
    </svg>
  )
}

const READS = [
  { id: 'ec2', name: 'EC2', icon: 'server' },
  { id: 'asg', name: 'Auto Scaling', icon: 'asg' },
  { id: 'elb', name: 'ALB / NLB', icon: 'lb' },
  { id: 'rds', name: 'RDS', icon: 'db' },
  { id: 'deploy', name: 'CodeDeploy', icon: 'deploy' },
]

// The hero's bottom row when it shows AWS, in the channel row's place and
// shape. In an outage it lights up the same way.
export function AwsRelay({ down }) {
  return (
    <div className={`live-channels landing-relay${down ? ' is-sending' : ''}`}>
      <span className="live-channels-label">
        {down
          ? 'Outage detected · one alert for the resource, not one per check'
          : 'Read from your AWS accounts, and kept in sync'}
      </span>
      <div className="live-channels-list">
        {READS.map((r, i) => (
          <div
            key={r.id}
            className={down ? 'live-channel is-sent' : 'live-channel'}
            style={{ '--order': i }}
          >
            <Glyph name={r.icon} />
            {r.name}
          </div>
        ))}
      </div>
    </div>
  )
}

// The hero's corner readouts when it shows AWS, in NightReadouts' place and
// shape: an Auto Scaling group and its targets playing out the moods.
const AWS_READOUTS = {
  up: { group: '4 / 4', targets: '3 / 3', alerts: 'None' },
  slow: { group: '3 / 4', targets: '2 / 3', alerts: 'Warning' },
  down: { group: '0 / 4', targets: '0 / 3', alerts: 'Sent' },
  maint: { group: 'Deploying', targets: 'Draining', alerts: 'Held' },
}

export function AwsReadouts({ mood }) {
  const readout = AWS_READOUTS[mood]
  return (
    <div className="night-readouts" aria-hidden="true">
      <div className="night-read is-tl">
        In service<b>{readout.group}</b>
      </div>
      <div className="night-read is-tr">
        Healthy targets<b>{readout.targets}</b>
      </div>
      <div className="night-read is-bl">
        VPC<b>prod-vpc</b>
      </div>
      <div className="night-read is-br">
        Alerts<b>{readout.alerts}</b>
      </div>
    </div>
  )
}

// A VPC as the network map shows one: Watchly in the public subnet, probing
// into the private one. One instance fails its health check now and then, and
// comes back.
export function VpcMap() {
  return (
    <figure className="landing-map">
      <div className="landing-map-scroll">
        <svg
          viewBox="0 0 680 330"
          role="img"
          aria-label="Watchly, on an EC2 instance in prod-vpc's public subnet, probes an internal load balancer and its three instances, an internal network load balancer and an RDS database in the private subnet. One instance fails its health check, then recovers."
        >
          <rect className="lmap-node" x="16" y="16" width="110" height="34" rx="8" />
          <text x="71" y="37" textAnchor="middle">
            Internet
          </text>

          <rect className="lmap-vpc" x="8" y="66" width="664" height="256" rx="12" />
          <text className="lmap-vpc-name" x="22" y="86">
            prod-vpc · 10.20.0.0/16
          </text>
          <rect className="lmap-subnet" x="20" y="96" width="170" height="214" rx="8" />
          <text className="lmap-note" x="30" y="114">
            public 10.20.0.0/24
          </text>
          <rect className="lmap-subnet" x="204" y="96" width="456" height="214" rx="8" />
          <text className="lmap-note" x="214" y="114">
            private 10.20.21.0/24
          </text>

          <path className="lmap-edge" d="M71 50 V170" />
          <rect className="lmap-node is-watchly" x="34" y="170" width="142" height="54" rx="8" />
          <circle className="lmap-led" cx="50" cy="188" r="4" />
          <text x="60" y="191">
            watchly
          </text>
          <text className="lmap-note" x="50" y="211">
            EC2 · 10.20.0.25
          </text>

          <rect className="lmap-node" x="236" y="128" width="130" height="44" rx="8" />
          <circle className="lmap-led" cx="252" cy="146" r="4" />
          <text x="262" y="149">
            orders-api
          </text>
          <text className="lmap-note" x="252" y="164">
            internal ALB
          </text>

          <path className="lmap-edge" d="M366 150 H400" />
          <rect className="lmap-node" x="400" y="128" width="104" height="44" rx="8" />
          <text x="412" y="149">
            orders-tg
          </text>
          <text className="lmap-note" x="412" y="164">
            /health :8080
          </text>

          <path
            className="lmap-edge"
            d="M504 150 C522 150 522 137 540 137 M504 150 C522 150 522 175 540 175 M504 150 C522 150 522 213 540 213"
          />
          <rect className="lmap-node" x="540" y="122" width="108" height="30" rx="6" />
          <circle className="lmap-led" cx="554" cy="137" r="4" />
          <text x="564" y="141">
            i-0a1c…
          </text>
          <rect className="lmap-node is-flaky" x="540" y="160" width="108" height="30" rx="6" />
          <circle className="lmap-led is-flaky" cx="554" cy="175" r="4" />
          <text x="564" y="179">
            i-07f3…
          </text>
          <rect className="lmap-node" x="540" y="198" width="108" height="30" rx="6" />
          <circle className="lmap-led" cx="554" cy="213" r="4" />
          <text x="564" y="217">
            i-0d92…
          </text>

          <rect className="lmap-node" x="236" y="200" width="130" height="44" rx="8" />
          <circle className="lmap-led" cx="252" cy="218" r="4" />
          <text x="262" y="221">
            ledger-tcp
          </text>
          <text className="lmap-note" x="252" y="236">
            internal NLB :7000
          </text>

          <rect className="lmap-node" x="400" y="250" width="150" height="44" rx="8" />
          <circle className="lmap-led" cx="416" cy="268" r="4" />
          <text x="426" y="271">
            orders-db
          </text>
          <text className="lmap-note" x="416" y="286">
            RDS postgres :5432
          </text>

          <path className="lmap-probe" d="M176 186 C210 186 210 150 236 150" />
          <path className="lmap-probe" d="M176 200 C210 200 210 222 236 222" />
          <path className="lmap-probe" d="M176 212 C300 300 330 272 400 272" />
          <path className="lmap-probe is-flaky" d="M176 194 C330 330 520 300 594 190" />
        </svg>
      </div>
      <figcaption className="muted small">
        Probes leave from inside the VPC, so private servers, internal load balancers and
        databases need no public address. Each target&apos;s security group just lets Watchly in.
      </figcaption>
    </figure>
  )
}

// What forty checks timing out at once looks like, one alert a check, against
// the one vpc_unreachable alert Watchly sends.
const STORM = [
  'orders-api /health',
  'i-0a1c… ping',
  'i-07f3… tcp 8080',
  'ledger-tcp :7000',
  'orders-db tcp 5432',
  'report-runner :8080',
  'i-0d92… /health',
  'bastion ssh 22',
]
const STORM_SIZE = 40

export function OneAlertNotForty() {
  return (
    <div className="landing-storm">
      <div className="landing-storm-head">
        <h3>A whole VPC drops: one alert, not forty</h3>
        <p className="muted">
          When Watchly loses its route into a VPC, every check there times out on the same tick.
          The project hears it once, with how many checks could not connect and the usual causes.
          Each resource&apos;s own alert waits until the VPC answers again.
        </p>
      </div>
      <div className="landing-storm-sides">
        <div className="landing-storm-side is-noise" aria-hidden="true">
          <span className="landing-storm-tag">An alert per check</span>
          <span className="landing-storm-count">{STORM_SIZE}</span>
          <ol>
            {Array.from({ length: STORM_SIZE }, (_, i) => (
              <li key={i}>
                DOWN · {STORM[i % STORM.length]} timed out · 09:42:
                {String((i * 7) % 60).padStart(2, '0')}
              </li>
            ))}
          </ol>
        </div>
        <div className="landing-storm-side">
          <span className="landing-storm-tag">Watchly</span>
          <div className="preview-slack">
            <div className="preview-slack-channel"># ops</div>
            <div className="preview-slack-msg">
              <span className="preview-avatar is-app is-logo" aria-hidden="true">
                <img src="/favicon.svg" alt="" />
              </span>
              <div className="preview-slack-body">
                <span>
                  <strong>Watchly</strong> <span className="preview-app">APP</span>{' '}
                  <span className="muted">09:42</span>
                </span>
                <strong className="preview-title">prod-vpc is unreachable</strong>
                <span className="muted small">VPC unreachable</span>
                <span className="preview-fields">
                  <span>
                    <b>Ranges</b>10.20.0.0/16
                  </span>
                  <span>
                    <b>Checks failing to connect</b>38 of 41
                  </span>
                </span>
                <span className="muted small">
                  A security group, NACL, route or peering change is the usual cause. Each
                  resource&apos;s own down alert is held until the VPC answers again.
                </span>
                <span className="preview-thread">1 reply · prod-vpc answers again, after 6m 12s</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

// A CodeDeploy deployment on a strip of checks: inside its window the checks
// still run and are kept, and a failing one alerts nobody.
const TICKS = 30
const WINDOW = [11, 22]
const FAILED = new Set([14, 15, 18])

export function DeployQuiet() {
  return (
    <div className="landing-deploy">
      <div className="landing-deploy-head">
        <h3>Deploys don&apos;t page anyone</h3>
        <p className="muted">
          Watchly sees a CodeDeploy deployment start, tells the project, and holds down alerts for
          the servers, groups and load balancers it touches. Checks keep running, so the history
          stays true. When it ends, anything still down alerts as usual.
        </p>
      </div>
      <div className="landing-deploy-board">
        <div className="landing-deploy-track" aria-hidden="true">
          {Array.from({ length: TICKS }, (_, i) => {
            const held = i >= WINDOW[0] && i < WINDOW[1]
            const cls = FAILED.has(i) ? 'is-failed' : held ? 'is-held' : ''
            return <i key={i} className={cls} />
          })}
          <span
            className="landing-deploy-window"
            style={{ '--from': WINDOW[0], '--span': WINDOW[1] - WINDOW[0], '--ticks': TICKS }}
          >
            <span>d-7X2K9QBFA · down alerts held</span>
          </span>
        </div>
        <div className="landing-deploy-legend muted small">
          <span>
            <i /> check passed
          </span>
          <span>
            <i className="is-failed" /> check failed, nobody paged
          </span>
        </div>
        <div className="landing-deploy-msgs">
          <div>
            <span className="landing-deploy-kicker">Deploying</span>
            <strong>Deploying orders-api to production</strong>
            <span className="muted small">
              Down alerts for 3 servers and 1 load balancer are paused until it ends.
            </span>
          </div>
          <div>
            <span className="landing-deploy-kicker is-up">Deployed · in the same thread</span>
            <strong>orders-api to production succeeded</strong>
            <span className="muted small">In 11m 4s. Checks of its resources resume in 1m.</span>
          </div>
        </div>
      </div>
    </div>
  )
}
