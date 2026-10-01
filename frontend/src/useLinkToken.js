import { useEffect, useState } from 'react'

/**
 * The one-time token from an emailed link, e.g. `/accept-invite#token=…`.
 *
 * The token rides in the URL fragment because browsers never send a fragment
 * to the server, so it stays out of access logs, proxies and `Referer`
 * headers. Links emailed before that change carry `?token=…` instead; those
 * still work. Either way the token is read once and then removed from the
 * address bar and history, so it is not left in a screenshot, a shared URL or
 * the browser's history.
 */
export function useLinkToken() {
  const [token] = useState(() => {
    const fromHash = new URLSearchParams(window.location.hash.replace(/^#/, '')).get('token')
    return fromHash ?? new URLSearchParams(window.location.search).get('token') ?? ''
  })

  useEffect(() => {
    const url = new URL(window.location.href)
    const hash = new URLSearchParams(url.hash.replace(/^#/, ''))
    if (!hash.has('token') && !url.searchParams.has('token')) return
    hash.delete('token')
    url.searchParams.delete('token')
    const rest = hash.toString()
    url.hash = rest ? `#${rest}` : ''
    window.history.replaceState(window.history.state, '', url)
  }, [])

  return token
}
