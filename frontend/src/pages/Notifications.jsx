import { Link } from 'react-router-dom'
import { useAuth } from '../auth.jsx'
import { PageHeader } from '../components.jsx'
import NotificationSettings from '../NotificationSettings.jsx'
import { canEditNotificationDefaults } from '../roles.js'

export default function Notifications() {
  const { user } = useAuth()
  const canEdit = canEditNotificationDefaults(user)

  return (
    <>
      <PageHeader
        title="Notifications"
        subtitle="The emails, Slack and Telegram messages Watchly sends, and the defaults every project inherits."
      />
      <div className="banner banner-info">
        Changes here apply to every project that has not set its own. A project can override any of
        this on its own page under <Link to="/projects">Projects</Link>. The monthly uptime report
        goes out on the 1st of each month, for the month before.
      </div>
      <NotificationSettings canEdit={canEdit} />
    </>
  )
}
