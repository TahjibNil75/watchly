import { useSyncExternalStore } from 'react'

// The app's theme is a choice of 'system' (follow the OS), 'light', 'dark' or
// 'night'. The page always carries a resolved theme as <html data-theme>, so
// the stylesheet only ever matches on that. index.html applies it before first
// paint with the same logic as applyTheme(); keep the two in step.
export const THEME_KEY = 'watchly.theme'

export const THEMES = [
  { value: 'system', label: 'Auto' },
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
  { value: 'night', label: 'Night' },
]

const VALUES = THEMES.map((theme) => theme.value)
const listeners = new Set()
const query = window.matchMedia('(prefers-color-scheme: dark)')

export function readTheme() {
  try {
    const saved = localStorage.getItem(THEME_KEY)
    return VALUES.includes(saved) ? saved : 'system'
  } catch {
    return 'system'
  }
}

let current = readTheme()

function applyTheme() {
  const resolved = current === 'system' ? (query.matches ? 'dark' : 'light') : current
  document.documentElement.dataset.theme = resolved
}

export function setTheme(next) {
  current = next
  try {
    localStorage.setItem(THEME_KEY, next)
  } catch {
    // Private mode: the choice just lasts until reload.
  }
  applyTheme()
  listeners.forEach((listener) => listener())
}

// Follow the OS while on Auto, and other tabs' choices.
query.addEventListener('change', applyTheme)
window.addEventListener('storage', (event) => {
  if (event.key !== THEME_KEY) return
  current = readTheme()
  applyTheme()
  listeners.forEach((listener) => listener())
})
applyTheme()

function subscribe(listener) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function useTheme() {
  return [useSyncExternalStore(subscribe, () => current), setTheme]
}
