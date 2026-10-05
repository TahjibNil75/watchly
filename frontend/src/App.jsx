import { Link, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { useAuth } from './auth.jsx'
import AuthLayout from './AuthLayout.jsx'
import { Empty, Loading } from './components.jsx'
import Layout from './Layout.jsx'
import AcceptInvite from './pages/AcceptInvite.jsx'
import ChoosePassword from './pages/ChoosePassword.jsx'
import ConfirmEmail from './pages/ConfirmEmail.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Infrastructure from './pages/Infrastructure.jsx'
import InfraResourceDetail from './pages/InfraResourceDetail.jsx'
import InfraSettings from './pages/InfraSettings.jsx'
import InfraVpc from './pages/InfraVpc.jsx'
import ForgotPassword from './pages/ForgotPassword.jsx'
import Landing from './pages/Landing.jsx'
import Login from './pages/Login.jsx'
import NewInfraResource from './pages/NewInfraResource.jsx'
import NewWebsite from './pages/NewWebsite.jsx'
import Notifications from './pages/Notifications.jsx'
import Profile from './pages/Profile.jsx'
import ProjectDetail from './pages/ProjectDetail.jsx'
import Projects from './pages/Projects.jsx'
import RequestAccount from './pages/RequestAccount.jsx'
import Signup from './pages/Signup.jsx'
import Users from './pages/Users.jsx'
import WebsiteDetail from './pages/WebsiteDetail.jsx'

function NotFound() {
  return (
    <Empty>
      Page not found. <Link to="/">Back to websites</Link>
    </Empty>
  )
}

export default function App() {
  const { user, checking } = useAuth()
  const location = useLocation()

  if (checking) {
    return (
      <div className="auth-screen">
        <Loading />
      </div>
    )
  }

  if (!user) {
    return (
      <Routes>
        {/* The site's root introduces Watchly; any other page asks to sign in,
            then returns there. */}
        <Route index element={<Landing />} />
        {/* Sign in is a full-screen page of its own, without the demo board. */}
        <Route path="/login" element={<Login />} />
        <Route element={<AuthLayout />}>
          <Route path="/signup" element={<Signup />} />
          <Route path="/forgot-password" element={<ForgotPassword />} />
          <Route path="/request-account" element={<RequestAccount />} />
          <Route path="/accept-invite" element={<AcceptInvite />} />
          <Route path="/confirm-email" element={<ConfirmEmail />} />
        </Route>
        <Route path="*" element={<Navigate to="/login" replace state={{ from: location }} />} />
      </Routes>
    )
  }

  // Signed in with a temporary password: the API allows nothing else until a
  // new one is chosen.
  if (user.must_change_password) return <ChoosePassword />

  return (
    <Routes>
      {/* Outside the layout: whoever opens the link may be someone else's browser
          session, and accepting replaces it with the new account. */}
      <Route element={<AuthLayout />}>
        <Route path="accept-invite" element={<AcceptInvite />} />
        {/* Outside too: the link is opened from an email, maybe in a browser
            signed in as someone else. */}
        <Route path="confirm-email" element={<ConfirmEmail />} />
      </Route>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="websites/new" element={<NewWebsite />} />
        <Route path="websites/:id" element={<WebsiteDetail />} />
        <Route path="infra" element={<Infrastructure />} />
        <Route path="infra/new" element={<NewInfraResource />} />
        <Route path="infra/settings" element={<InfraSettings />} />
        <Route path="infra/resources/:id" element={<InfraResourceDetail />} />
        <Route path="infra/vpcs/:id" element={<InfraVpc />} />
        <Route path="projects" element={<Projects />} />
        <Route path="projects/:id" element={<ProjectDetail />} />
        <Route path="notifications" element={<Notifications />} />
        <Route path="users" element={<Users />} />
        <Route path="profile" element={<Profile />} />
        <Route path="login" element={<Navigate to="/" replace />} />
        <Route path="signup" element={<Navigate to="/" replace />} />
        <Route path="forgot-password" element={<Navigate to="/" replace />} />
        <Route path="request-account" element={<Navigate to="/" replace />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  )
}
