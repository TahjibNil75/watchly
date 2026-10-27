import { useEffect, useState } from 'react'
import { api } from './api.js'

// A site's favicon, so a long list can be scanned by logo and colour. Watchly's
// own API fetches and caches the icon (the browser never contacts the site or a
// favicon service), and it needs the bearer token, so the image is fetched here
// and shown from an object URL. A site with no icon, and any check that is not
// a web page, gets a tile with its first letter in a colour taken from its name.

// key -> the object URL, or null for "has none". Kept for the session so a list
// re-rendered or revisited shows its icons at once.
const known = new Map()
const pending = new Map()

const keyOf = (site) => `${site.id}|${site.url}`

function load(site) {
  const key = keyOf(site)
  if (!pending.has(key)) {
    pending.set(
      key,
      api
        .websiteFavicon(site.id)
        .then((blob) => {
          const src = blob ? URL.createObjectURL(blob) : null
          known.set(key, src)
          return src
        })
        // Not remembered, so a blip retries the next time it is shown.
        .catch(() => null)
        .finally(() => pending.delete(key)),
    )
  }
  return pending.get(key)
}

// A stable hue from the name, so a site keeps its colour.
function hueOf(text) {
  let hash = 0
  for (const ch of text) hash = (hash * 31 + ch.codePointAt(0)) % 360
  return hash
}

const hasPage = (site) => (site.check_type ?? 'http') === 'http'

export default function SiteIcon({ site, className = '' }) {
  const key = keyOf(site)
  const [src, setSrc] = useState(() => known.get(key) ?? null)
  const wanted = hasPage(site)

  useEffect(() => {
    if (!wanted) return undefined
    let live = true
    load({ id: site.id, url: site.url }).then((found) => {
      if (live) setSrc(found)
    })
    return () => {
      live = false
    }
  }, [wanted, site.id, site.url])

  if (wanted && src) {
    return (
      <img
        src={src}
        alt=""
        className={`site-icon ${className}`}
        onError={() => setSrc(null)}
      />
    )
  }
  return (
    <span
      className={`site-icon site-icon-fallback ${className}`}
      style={{ '--hue': hueOf(site.name) }}
      aria-hidden="true"
    >
      {(site.name.trim()[0] ?? '?').toUpperCase()}
    </span>
  )
}
