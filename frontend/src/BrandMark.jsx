// Watchly's mark: the green status dot, grown a face and a pair of glasses.
// Its big eyes keep glancing about, as a watcher should, and blink now and
// then. The motion lives in index.css (.brand-mark), so the mark stays still
// for visitors who ask for reduced motion.

// 32x32. The face follows --up; the ink and eye whites are fixed, so the eyes
// read on either theme's green.
export default function BrandMark() {
  return (
    <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
      <circle className="brand-face" cx="16" cy="16" r="15" />
      <g className="brand-features">
        <g className="brand-eyes">
          <circle className="brand-white" cx="10" cy="13.4" r="5" />
          <circle className="brand-white" cx="22" cy="13.4" r="5" />
          <g className="brand-pupils">
            <circle className="brand-ink" cx="10" cy="13.4" r="2.7" />
            <circle className="brand-ink" cx="22" cy="13.4" r="2.7" />
            <circle className="brand-white" cx="11" cy="12.4" r="0.9" />
            <circle className="brand-white" cx="23" cy="12.4" r="0.9" />
          </g>
        </g>
        <g className="brand-glasses">
          <circle cx="10" cy="13.4" r="5.5" />
          <circle cx="22" cy="13.4" r="5.5" />
          <path d="M15.2 12.2q.8-.8 1.6 0M4.5 12.6H3M27.5 12.6H29" />
        </g>
        <path className="brand-glint" d="M12.6 9.9a4 4 0 0 1 1.3 1.8M24.6 9.9a4 4 0 0 1 1.3 1.8" />
        <path className="brand-mouth" d="M13.8 23q2.2 1.6 4.4 0" />
      </g>
    </svg>
  )
}
