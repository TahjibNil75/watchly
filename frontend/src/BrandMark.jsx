import { useEffect, useId, useState } from 'react'
import { useMediaQuery } from './useMediaQuery.js'

// Watchly's mark: the status dot, grown a face and a pair of round glasses too
// big for it, hanging on to the top of a browser window and peering over at
// the site it watches. The window shows the site's pulse. Its head and eyes
// keep glancing about, as a watcher should, and it blinks now and then. The
// motion lives in index.css (.brand-mark).
//
// It plays through a site's moods, one every few seconds:
//   up    green and a little happy: a heartbeat on the screen and a hop now
//         and then
//   slow  yellow and fed up with waiting: heavy lids, a slouch, a droop and a
//         lazy wave on the screen
//   down  red and tense: slanted lids, pinpoint pupils darting side to side,
//         a shiver, a bead of sweat, and a flatline on the screen
//   maint purple and busy tinkering: eyes down on the window, a wrench
//         tapping in one hand and a gear turning on the screen
// Given a `mood`, the mark holds that one instead, as it does on an alert.
// Every mood's lids and trace are drawn, and CSS shows the current one's, so
// a change of mood eases from one face to the next. Visitors who ask for
// reduced motion get the happy face, still.

const MOODS = ['up', 'slow', 'down', 'maint']
const MOOD_MS = 5000

// Read off the clock, so every mark on the page shows the same mood and moving
// between pages doesn't start the round again.
const moodNow = () => MOODS[Math.floor(Date.now() / MOOD_MS) % MOODS.length]

function useMood(still) {
  const [mood, setMood] = useState(moodNow)
  useEffect(() => {
    if (still) return undefined
    let timer
    const tick = () => {
      setMood(moodNow())
      timer = setTimeout(tick, MOOD_MS - (Date.now() % MOOD_MS))
    }
    tick()
    return () => clearTimeout(timer)
  }, [still])
  return still ? 'up' : mood
}

// The mood a mark shows: `fixed` if given, else the one the clock is on. For
// pages that dress to match the mark, as the sign-in page does.
export function useBrandMood(fixed) {
  const cycling = useMood(useMediaQuery('(prefers-reduced-motion: reduce)') || Boolean(fixed))
  return fixed ?? cycling
}

// 32x32: the head up top, its glasses resting on the window's top edge and its
// hands on the corners. The ink, eye whites and window are fixed, so they read
// on every face and either theme. The clip id is per instance: a url(#id) that
// lands on a copy inside a hidden element (the drawer's top bar on wide
// screens) clips to nothing.
export default function BrandMark({ mood: fixed }) {
  const mood = useBrandMood(fixed)
  const lens = `${useId()}lens`
  return (
    <svg className={`brand-mark is-${mood}`} viewBox="0 0 32 32" aria-hidden="true">
      <defs>
        <clipPath id={lens}>
          <circle cx="9.4" cy="11.6" r="5.4" />
          <circle cx="22.6" cy="11.6" r="5.4" />
        </clipPath>
      </defs>
      <g className="brand-rise">
        <g className="brand-head">
          <g className="brand-bounce">
            <circle className="brand-skin" cx="16" cy="15.5" r="11.5" />
            <g className="brand-features">
              <circle className="brand-skin" cx="9.4" cy="11.6" r="5.5" />
              <circle className="brand-skin" cx="22.6" cy="11.6" r="5.5" />
              <g className="brand-eyes">
                <circle className="brand-white" cx="9.4" cy="11.6" r="5.5" />
                <circle className="brand-white" cx="22.6" cy="11.6" r="5.5" />
                <g className="brand-pupils">
                  <circle className="brand-pupil" cx="9.4" cy="11.6" r="3" />
                  <circle className="brand-pupil" cx="22.6" cy="11.6" r="3" />
                  <circle className="brand-white" cx="10.55" cy="10.45" r="1.05" />
                  <circle className="brand-white" cx="23.75" cy="10.45" r="1.05" />
                </g>
              </g>
              <g clipPath={`url(#${lens})`}>
                <rect className="brand-skin brand-lid brand-lid-l" x="1" y="-2.8" width="15" height="9" />
                <rect className="brand-skin brand-lid brand-lid-r" x="16" y="-2.8" width="15" height="9" />
              </g>
              <g className="brand-glasses">
                <circle cx="9.4" cy="11.6" r="6.2" />
                <circle cx="22.6" cy="11.6" r="6.2" />
                <path d="M15.3 9.9q.7-.7 1.4 0" />
              </g>
            </g>
            <path className="brand-sweat" d="M29.2 1.2q-2 3-2 4.3a2 2 0 0 0 4 0q0-1.3-2-4.3z" />
          </g>
        </g>
      </g>
      <rect className="brand-window" x="1.6" y="18.6" width="28.8" height="12" rx="2.4" />
      <g className="brand-chrome">
        <circle cx="4.6" cy="21.3" r="0.8" />
        <circle cx="6.9" cy="21.3" r="0.8" />
        <circle cx="9.2" cy="21.3" r="0.8" />
      </g>
      <path className="brand-trace brand-trace-up" pathLength="1" d="M4.6 26.4h7.2l1.5-3.1 2.1 5.5 1.7-4.2 1.2 1.8h9.1" />
      <path className="brand-trace brand-trace-slow" pathLength="1" d="M4.6 26.4h8.8q1.7-2.4 3.4 0t3.4 0h7.2" />
      <path className="brand-trace brand-trace-down" d="M4.6 26.4h22.8" />
      <g className="brand-gear">
        <g className="brand-gear-spin">
          <circle cx="16" cy="25.6" r="3.8" strokeWidth="2" strokeDasharray="1.5 1.4" />
          <circle cx="16" cy="25.6" r="2.7" strokeWidth="1.4" />
          <circle className="brand-gear-hub" cx="16" cy="25.6" r="1" />
        </g>
      </g>
      <g className="brand-wrench">
        <path className="brand-wrench-ink" d="M27.7 18.8L30.4 13.4" />
        <path className="brand-wrench-body" d="M27.7 18.8L30.4 13.4" />
        <circle className="brand-wrench-jaw" cx="30.7" cy="12.9" r="1.9" />
        <path className="brand-wrench-slot" d="M30.1 10.8l.6 1.4 1.2-.2" />
      </g>
      <circle className="brand-skin brand-hand" cx="4.3" cy="18.8" r="1.9" />
      <circle className="brand-skin brand-hand" cx="27.7" cy="18.8" r="1.9" />
    </svg>
  )
}
