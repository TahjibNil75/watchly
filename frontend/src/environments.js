// Mirrors WebsiteEnvironment in app/monitoring/websites/models.py, in the order
// the dropdown lists them. The API speaks the lowercase values.
export const ENVIRONMENTS = [
  { value: 'development', label: 'Development' },
  { value: 'testing', label: 'Testing' },
  { value: 'uat', label: 'UAT' },
  { value: 'staging', label: 'Staging' },
  { value: 'production', label: 'Production' },
]

export const environmentLabel = (value) => ENVIRONMENTS.find((e) => e.value === value)?.label ?? null
