import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { api, getToken, setToken, setUnauthorizedHandler } from './api.js'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  // With a stored token we don't know who is signed in until /users/me answers.
  const [checking, setChecking] = useState(() => Boolean(getToken()))

  // Forget the session on this side, for when the API says it is over.
  const forget = useCallback(() => {
    setToken(null)
    setUser(null)
  }, [])

  // Signing out also revokes the session in the refresh cookie, which only
  // the API can clear. The access token can't be revoked; it just lapses.
  const logout = useCallback(() => {
    api.logout().catch(() => {})
    forget()
  }, [forget])

  useEffect(() => {
    setUnauthorizedHandler(forget)
    if (!getToken()) return
    api
      .me()
      .then(setUser)
      .catch(forget)
      .finally(() => setChecking(false))
  }, [forget])

  const value = useMemo(() => {
    const accept = ({ user, tokens }) => {
      setToken(tokens.access_token)
      setUser(user)
    }
    return {
      user,
      checking,
      logout,
      // For pages that change the signed-in user, e.g. the profile.
      updateUser: setUser,
      login: async (identifier, password) => accept(await api.login(identifier, password)),
      signup: async (payload) => accept(await api.signup(payload)),
      acceptInvitation: async (payload) => accept(await api.acceptInvitation(payload)),
    }
  }, [user, checking, logout])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

// eslint-disable-next-line react/only-export-components
export function useAuth() {
  return useContext(AuthContext)
}
