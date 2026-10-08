import { useSyncExternalStore } from 'react'

// Display preferences, kept in this browser like the theme: a time zone, a 12
// or 24 hour clock, and how tightly tables and cards are packed. 'auto' means
// whatever the device says. Density is also applied to <html data-density> by
// index.html before first paint; keep the two in step.
const KEY = 'watchly.prefs'
const DEFAULTS = { timeZone: 'auto', hours: 'auto', density: 'comfortable' }

export const HOUR_CHOICES = [
  { value: 'auto', label: 'Auto' },
  { value: '12', label: '12-hour' },
  { value: '24', label: '24-hour' },
]

export const DENSITY_CHOICES = [
  { value: 'comfortable', label: 'Comfortable' },
  { value: 'compact', label: 'Compact' },
]

export const deviceZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone

export function isZone(name) {
  try {
    new Intl.DateTimeFormat(undefined, { timeZone: name })
    return true
  } catch {
    return false
  }
}

function read() {
  try {
    const saved = JSON.parse(localStorage.getItem(KEY))
    if (!saved || typeof saved !== 'object') return DEFAULTS
    return {
      timeZone: saved.timeZone === 'auto' || isZone(saved.timeZone) ? saved.timeZone : DEFAULTS.timeZone,
      hours: HOUR_CHOICES.some((c) => c.value === saved.hours) ? saved.hours : DEFAULTS.hours,
      density: DENSITY_CHOICES.some((c) => c.value === saved.density) ? saved.density : DEFAULTS.density,
    }
  } catch {
    return DEFAULTS
  }
}

let current = read()
const listeners = new Set()

function apply() {
  document.documentElement.dataset.density = current.density
}

function publish() {
  apply()
  listeners.forEach((listener) => listener())
}

export const getPrefs = () => current

export function setPrefs(patch) {
  current = { ...current, ...patch }
  try {
    localStorage.setItem(KEY, JSON.stringify(current))
  } catch {
    // Private mode: the choice just lasts until reload.
  }
  publish()
}

window.addEventListener('storage', (event) => {
  if (event.key !== KEY) return
  current = read()
  publish()
})
apply()

function subscribe(listener) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function usePrefs() {
  return [useSyncExternalStore(subscribe, getPrefs), setPrefs]
}

// The zone times are shown in: the chosen one, else the device's.
export const activeZone = () => (current.timeZone === 'auto' ? deviceZone() : current.timeZone)

// Intl options for the current choices, merged into any formatter.
export function zoneOptions() {
  const options = {}
  if (current.timeZone !== 'auto') options.timeZone = current.timeZone
  if (current.hours !== 'auto') options.hourCycle = current.hours === '12' ? 'h12' : 'h23'
  return options
}

// Every zone the browser knows, for the picker; a short list where it cannot say.
export function zoneNames() {
  try {
    const names = Intl.supportedValuesOf('timeZone')
    return names.includes('UTC') ? names : ['UTC', ...names]
  } catch {
    return ['UTC']
  }
}
