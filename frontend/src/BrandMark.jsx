import { useEffect, useId, useState } from 'react'
import { useMediaQuery } from './useMediaQuery.js'

// Watchly's mark: the status dot, grown a face and a pair of round glasses too
// big for it. Its head and eyes keep glancing about, as a watcher should, and
// it blinks now and then. The motion lives in index.css (.brand-mark).
//
// It plays through a site's moods, one every few seconds:
//   up    green and a little happy: a grin and a hop now and then
//   slow  yellow and fed up with waiting: heavy lids, a flat mouth, a droop
//   down  red and tense: slanted lids, pinpoint pupils darting side to side,
//         a wobbly mouth, a shiver and a bead of sweat
// Every mood's lids and mouth are drawn, and CSS shows the current one's, so
// a change of mood eases from one face to the next. Visitors who ask for
// reduced motion get the happy face, still.

const MOODS = ['up', 'slow', 'down']
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

// 32x32, the lenses reaching past the head on both sides. The ink and eye
// whites are fixed, so the eyes read on every face. The clip id is per
// instance: a url(#id) that lands on a copy inside a hidden element (the
// drawer's top bar on wide screens) clips to nothing.
export default function BrandMark() {
  const mood = useMood(useMediaQuery('(prefers-reduced-motion: reduce)'))
  const lens = `${useId()}lens`
  return (
    <svg className={`brand-mark is-${mood}`} viewBox="0 0 32 32" aria-hidden="true">
      <defs>
        <clipPath id={lens}>
          <circle cx="8.4" cy="14.8" r="6.2" />
          <circle cx="23.6" cy="14.8" r="6.2" />
        </clipPath>
      </defs>
      <g className="brand-head">
        <g className="brand-bounce">
          <circle className="brand-skin" cx="16" cy="18" r="12.5" />
          <g className="brand-features">
            <circle className="brand-skin" cx="8.4" cy="14.8" r="6.3" />
            <circle className="brand-skin" cx="23.6" cy="14.8" r="6.3" />
            <g className="brand-eyes">
              <circle className="brand-white" cx="8.4" cy="14.8" r="6.3" />
              <circle className="brand-white" cx="23.6" cy="14.8" r="6.3" />
              <g className="brand-pupils">
                <circle className="brand-pupil" cx="8.4" cy="14.8" r="3.4" />
                <circle className="brand-pupil" cx="23.6" cy="14.8" r="3.4" />
                <circle className="brand-white" cx="9.7" cy="13.5" r="1.2" />
                <circle className="brand-white" cx="24.9" cy="13.5" r="1.2" />
                <circle className="brand-white" cx="7.3" cy="16.1" r="0.55" />
                <circle className="brand-white" cx="22.5" cy="16.1" r="0.55" />
              </g>
            </g>
            <g clipPath={`url(#${lens})`}>
              <rect className="brand-skin brand-lid brand-lid-l" x="0.4" y="-0.6" width="16" height="9.2" />
              <rect className="brand-skin brand-lid brand-lid-r" x="15.6" y="-0.6" width="16" height="9.2" />
            </g>
            <g className="brand-glasses">
              <circle cx="8.4" cy="14.8" r="7.1" />
              <circle cx="23.6" cy="14.8" r="7.1" />
              <path d="M15.1 12.4q.9-.9 1.8 0" />
            </g>
            <path className="brand-grin" d="M12.6 25.2h6.8q-.4 3.8-3.4 3.8t-3.4-3.8z" />
            <path className="brand-mouth brand-flat" d="M13.2 27h5.6" />
            <path className="brand-mouth brand-wobble" d="M12.2 27q.95-1 1.9 0t1.9 0 1.9 0 1.9 0" />
          </g>
          <path className="brand-sweat" d="M29.2 1.6q-2 3-2 4.3a2 2 0 0 0 4 0q0-1.3-2-4.3z" />
        </g>
      </g>
    </svg>
  )
}
