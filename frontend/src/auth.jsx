import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { api, getToken, setToken, setUnauthorizedHandler } from './api.js'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  // With a stored token we don't know who is signed in until /users/me answers.
  const [checking, setChecking] = useState(() => Boolean(getToken()))

  // There is no logout endpoint: the client just forgets its token.
  const logout = useCallback(() => {
    setToken(null)
    setUser(null)
  }, [])

  useEffect(() => {
    setUnauthorizedHandler(logout)
    if (!getToken()) return
    api
      .me()
      .then(setUser)
      .catch(logout)
      .finally(() => setChecking(false))
  }, [logout])

  const value = useMemo(() => {
    const accept = ({ user, tokens }) => {
      setToken(tokens.access_token)
      setUser(user)
    }
    return {
      user,
      checking,
      logout,
      login: async (identifier, password) => accept(await api.login(identifier, password)),
      signup: async (payload) => accept(await api.signup(payload)),
    }
  }, [user, checking, logout])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

// eslint-disable-next-line react/only-export-components
export function useAuth() {
  return useContext(AuthContext)
}
