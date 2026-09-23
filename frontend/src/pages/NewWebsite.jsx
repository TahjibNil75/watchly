import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { Empty, ErrorBanner, Loading, PageHeader } from '../components.jsx'
import { canCreateProjects, canManageProject } from '../roles.js'
import { useApi } from '../useApi.js'
import WebsiteForm from '../WebsiteForm.jsx'

export default function NewWebsite() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const allowed = canCreateProjects(user)

  const projects = useApi(() => (allowed ? api.listProjects() : null), [allowed])
  const users = useApi(() => (allowed ? api.listUsers({ is_active: true }) : null), [allowed])

  if (!allowed) {
    return <Empty>Only admins, DevOps and project managers can add websites.</Empty>
  }
  if (projects.loading || users.loading) return <Loading />

  const manageable = (projects.data?.items ?? []).filter((p) => canManageProject(user, p))
  const requested = Number(params.get('project'))
  const defaultProjectId = manageable.some((p) => p.id === requested) ? requested : undefined

  return (
    <>
      <PageHeader
        title="Add website"
        subtitle="Watchly will poll this URL on its interval and alert the project when it stops answering."
      />
      <ErrorBanner error={projects.error} />
      {manageable.length === 0 ? (
        <Empty>
          Websites live under a project, and you don't manage any yet.{' '}
          <Link to="/projects">Create a project</Link> first.
        </Empty>
      ) : (
        <div className="card">
          <WebsiteForm
            projects={manageable}
            users={users.data?.items ?? []}
            defaultProjectId={defaultProjectId}
            submitLabel="Start monitoring"
            onCancel={() => navigate(-1)}
            onSubmit={async (payload) => {
              const site = await api.createWebsite(payload)
              navigate(`/websites/${site.id}`)
            }}
          />
        </div>
      )}
    </>
  )
}
