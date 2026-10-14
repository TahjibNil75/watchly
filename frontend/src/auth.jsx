import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { api, getToken, setToken, setUnauthorizedHandler, TOKEN_KEY } from './api.js'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  // With a stored token we don't know who is signed in until /users/me answers.
  const [checking, setChecking] = useState(() => Boolean(getToken()))

  // Set when /users/me failed for a reason other than the session ending (API
  // down, proxy 502). The token is kept so a retry can still succeed.
  const [checkError, setCheckError] = useState(null)
  const [attempt, setAttempt] = useState(0)

  // Forget the session on this side, for when the API says it is over.
  const forget = useCallback(() => {
    setToken(null)
    setUser(null)
    setCheckError(null)
  }, [])

  // Signing out also revokes the session in the refresh cookie, which only
  // the API can clear. The access token can't be revoked; it just lapses.
  const logout = useCallback(() => {
    api.logout().catch(() => {})
    forget()
  }, [forget])

  // Another tab signing out removes the token from storage; follow it.
  useEffect(() => {
    const onStorage = (event) => {
      // A null key is localStorage.clear().
      if (event.key !== TOKEN_KEY && event.key !== null) return
      if (!getToken()) forget()
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [forget])

  const retryCheck = useCallback(() => {
    setCheckError(null)
    setChecking(true)
    setAttempt((n) => n + 1)
  }, [])

  useEffect(() => {
    setUnauthorizedHandler(forget)
    if (!getToken()) return
    let live = true
    api
      .me()
      .then((me) => live && setUser(me))
      .catch((err) => {
        if (!live) return
        // Only a 401/403 means the session is over. request() has already
        // tried the refresh cookie for a 401.
        if (err.status === 401 || err.status === 403) forget()
        else setCheckError(err)
      })
      .finally(() => live && setChecking(false))
    return () => {
      live = false
    }
  }, [forget, attempt])

  const value = useMemo(() => {
    const accept = ({ user, tokens }) => {
      setToken(tokens.access_token)
      setUser(user)
    }
    return {
      user,
      checking,
      checkError,
      retryCheck,
      logout,
      // For pages that change the signed-in user, e.g. the profile.
      updateUser: setUser,
      login: async (identifier, password) => accept(await api.login(identifier, password)),
      signup: async (payload) => accept(await api.signup(payload)),
      acceptInvitation: async (payload) => accept(await api.acceptInvitation(payload)),
    }
  }, [user, checking, checkError, retryCheck, logout])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

// eslint-disable-next-line react/only-export-components
export function useAuth() {
  return useContext(AuthContext)
}

// Whether to offer "Create an account": true only on a fresh install, before
// the admin exists; everyone after joins by invitation. null until the API answers;
// if it can't be asked, assume open and let the form show the API's refusal.
// eslint-disable-next-line react/only-export-components
export function useSignupOpen() {
  const [open, setOpen] = useState(null)
  useEffect(() => {
    let live = true
    api
      .signupStatus()
      .then((status) => live && setOpen(status.open))
      .catch(() => live && setOpen(true))
    return () => {
      live = false
    }
  }, [])
  return open
}
