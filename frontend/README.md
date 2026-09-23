# Watchly frontend

A small React app (Vite + React Router) for the Watchly API: sign in, see
which sites are up or down, and manage projects, websites, alert recipients
and users.

## Run it

The quickest way is the whole stack in Docker, from the repo root:

```bash
docker compose up -d --build
```

The UI is then at http://localhost:8080. It's served by nginx (`nginx.conf`),
which also proxies `/api` and `/health` to the `api` container.

For development with hot reload, start the API first (see the [root README](../Readme.md)), then:

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 and sign in. The seeded admin is `admin` /
`Admin@123`.

The API has no CORS middleware, so in development Vite proxies `/api` and
`/health` to `http://127.0.0.1:8000`. To point at a different backend, copy
`.env.example` to `.env.local` and set `API_PROXY_TARGET`.

## Pages

| route | what it shows |
| ----- | ------------- |
| `/` | Every visible site with its status, filterable by status and project. Refreshes every 30s. |
| `/websites/:id` | One site: outage state, recent checks, "Check now", pause, edit, delete, and site recipients. |
| `/websites/new` | Add a site under a project you manage. |
| `/projects` | Projects with site counts, and a form to create one. |
| `/projects/:id` | A project's sites, members and alert settings (email and Slack). |
| `/users` | User directory: change roles, suspend or reactivate (admin, DevOps, project manager). |

Buttons only show up when your role allows the action. That logic in
`src/roles.js` mirrors `app/core/permissions.py`, but it only hides buttons. The
API still enforces every rule.

## Build

```bash
npm run build     # outputs dist/
npm run lint
```

`dist/` is a static bundle. Serve it from the same origin as the API, or set
`VITE_API_URL` at build time and add CORS to the API. Any route other than a
file must fall back to `index.html` so client-side routing works. The
`Dockerfile` and `nginx.conf` here already handle this.

## Known limits

- There is no refresh endpoint, so when the 30-minute access token expires the
  app sends you back to the sign-in page. It returns you to the page you were on
  once you sign in again.
- The token is kept in `localStorage`.
- List views load up to 100 items (the API's page limit) and say so when
  there are more.

## Layout

```
src/
  api.js            fetch wrapper, token storage, every endpoint
  auth.jsx          AuthProvider / useAuth
  useApi.js         load + poll hook
  roles.js          UI permission hints
  format.js         time and email helpers
  components.jsx    badges, tables, user pickers
  WebsiteForm.jsx   create/edit website
  ProjectForm.jsx   create/edit project
  Layout.jsx        left sidebar (nav, API health, user, Sign out); a drawer on phones
  pages/            one file per route
```
