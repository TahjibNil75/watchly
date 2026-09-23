import { useCallback, useEffect, useRef, useState } from 'react'

// Loads data with `load()` whenever `deps` change, optionally re-polling every
// `pollMs` while the tab is visible. Only the newest request may update state,
// so a slow response can never overwrite a fresher one.
export function useApi(load, deps, { pollMs } = {}) {
  const [state, setState] = useState({ data: null, error: null, loading: true })
  const latest = useRef(0)

  const reload = useCallback(async () => {
    const id = ++latest.current
    try {
      const data = await load()
      if (id === latest.current) setState({ data, error: null, loading: false })
    } catch (error) {
      if (id === latest.current) setState((prev) => ({ ...prev, error, loading: false }))
    }
    // The caller's deps decide when `load` is stale, as with useEffect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    reload()
    if (!pollMs) return undefined
    const timer = setInterval(() => {
      if (!document.hidden) reload()
    }, pollMs)
    return () => clearInterval(timer)
  }, [reload, pollMs])

  // Lets a mutation drop its response straight in without a refetch.
  const setData = useCallback((data) => {
    latest.current++
    setState({ data, error: null, loading: false })
  }, [])

  return { ...state, reload, setData }
}
