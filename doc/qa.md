# QA report: known bugs

Bugs found in a QA pass over the backend and frontend on 2026-10-14, at commit
`f8bac34`. Each one has the steps that reproduce it, what should happen, what
happens instead, and where the fault is in the code.

All of these were reproduced on a throwaway instance (its own database,
alerts off), except B8, which is from reading the code. The automated checks
all pass, so none of these is caught by them today:

| Check | Result |
| --- | --- |
| `pytest` | 63 passed |
| `python -c "import app.main"` | OK |
| `alembic upgrade head`, `alembic check`, `downgrade base` + `upgrade head` | OK, no drift |
| `npm run lint` (oxlint) | 0 errors, 4 warnings (fast-refresh exports, and one `useLayoutEffect` in `Select.jsx` that is guarded and not a bug) |
| `npm run build` | OK (one chunk over 500 kB) |
| Every endpoint in `frontend/src/api.js` against the live OpenAPI spec | All exist, with the right methods |

- [Summary](#summary)
- [Backend](#backend)
- [Frontend](#frontend)
- [Docs](#docs)
- [Not covered by this pass](#not-covered-by-this-pass)

---

## Summary

| ID | Severity | Area | Bug |
| --- | --- | --- | --- |
| [B1](#b1-one-site-that-redirects-to-a-long-url-stops-all-monitoring) | High | Backend | One site that redirects to a long URL stops all monitoring |
| [B2](#b2-inactive-projects-are-still-checked-and-still-alert) | Medium | Backend + UI | Inactive projects are still checked and still alert |
| [B3](#b3-signing-in-by-email-is-case-sensitive) | Medium | Backend | Signing in by email is case-sensitive |
| [B4](#b4-patch-project-with-name-null-or-is_active-null-returns-500) | Medium | Backend | `PATCH` project with `name: null` or `is_active: null` returns 500 |
| [B5](#b5-patch-website-with-method-null-returns-500) | Medium | Backend | `PATCH` website with `method: null` returns 500 |
| [B6](#b6-patch-website-with-null-on-a-required-field-returns-a-false-409) | Low | Backend | `PATCH` website with `null` on a required field returns a false 409 |
| [B7](#b7-resuming-a-site-paused-while-down-sends-a-recovered-alert-for-the-whole-pause) | Low | Backend | Resuming a site paused while down sends a "recovered" alert for the whole pause |
| [B8](#b8-a-lost-race-when-accepting-an-invitation-returns-500) | Low | Backend | A lost race when accepting an invitation returns 500 (code review only) |
| [F1](#f1-a-failed-usersme-on-page-load-signs-the-user-out) | Medium | Frontend | A failed `/users/me` on page load signs the user out |
| [F2](#f2-project-counts-and-lists-stop-at-the-first-page) | Medium | Frontend | Project counts and lists stop at the first page |
| [F3](#f3-signing-out-in-one-tab-leaves-other-tabs-half-signed-in) | Low | Frontend | Signing out in one tab leaves other tabs half signed in |
| [F4](#f4-the-projects-page-says-no-projects-yet-when-loading-failed) | Low | Frontend | The Projects page says "No projects yet" when loading failed |
| [D1](#d1-hldmd-and-referencemd-say-a-lockout-suspends-the-account) | Low | Docs | `hld.md` and `reference.md` say a lockout suspends the account |

---

## Backend

### B1. One site that redirects to a long URL stops all monitoring

**Severity:** High

**Where:** [app/monitoring/websites/checker.py:1024](../app/monitoring/websites/checker.py#L1024) stores `final_url=str(response.url)` without truncating it, into a `String(2048)` column ([models.py:742](../app/monitoring/websites/models.py#L742)). The website stage of the tick, [scheduler.py:68](../app/monitoring/scheduler.py#L68), is not wrapped in `_run_guarded` the way every other job is.

**Steps:**

1. Monitor an HTTP URL that answers `302` with a `Location` longer than 2048 characters, such as `/landing?pad=` followed by 3,000 `x` characters.
2. Click **Check now** on it, or let the scheduler reach it.

**Expected:** The check is recorded, with the final URL cut to fit or the site reported down.

**Actual:**

- **Check now** returns `500`: `value too long for type character varying(2048)`.
- On a scheduler tick the same error escapes `run_due_checks()` and aborts the whole tick. In the reproduction, two healthy sites that were due in the same tick were never recorded. Everything after the website stage was also skipped: infrastructure and Docker jobs, rollups, monthly reports and purges.
- The failing site never gets `last_checked_at`, so it stays first in the due list (`nulls_first`) and kills every following tick the same way. One misbehaving monitored server is enough to silently stop monitoring, and alerts, for the whole install.

**Fix:** Truncate `final_url` to 2048 characters, as the certificate and registrar fields already are (`[:255]`). Also run the website stage through `_run_guarded`, and catch errors per site in `run_due_checks` so one bad result can't drop the rest.

### B2. Inactive projects are still checked and still alert

**Severity:** Medium

**Where:** `WebsiteService.due_for_check` ([service.py:748](../app/monitoring/websites/service.py#L748)) filters only on `Website.is_enabled`. Nothing in the scheduler or in `notifications/dispatcher.py` looks at `Project.is_active`, which is only used by the project list filter and the monthly reports.

**Steps:**

1. Edit a project, untick **Project is active**, and save.
2. Leave one of its sites due, or take one of its sites down.

**Expected:** An inactive project's sites are not checked and send no alerts, or the checkbox explains what it actually does.

**Actual:** The scheduler's due list still includes the site (confirmed by calling `due_for_check()` on the test database), so it is probed and alerts go out as usual. At the same time the Projects page shows the card as grey **Inactive** and hides the red "down" ping ([Projects.jsx:86](../frontend/src/pages/Projects.jsx#L86), [Projects.jsx:122](../frontend/src/pages/Projects.jsx#L122)). The UI says the project is quiet while it is still paging people.

**Status:** Fixed. "Inactive" now means paused: `due_for_check`, `due_resources` and `mark_offline` skip the project (`active_project_ids()` in `projects/models.py`), `Notifier.dispatch` sends nothing for it (a report sent by hand excepted), and the checkbox says so. Covered by `tests/monitoring/test_inactive_project.py`.

**Fix:** Decide what "inactive" means. If it means paused, add `Project.is_active` to `due_for_check` (and to the infrastructure and Docker equivalents). If it is only a label, say so next to the checkbox and stop hiding outages on the card.

### B3. Signing in by email is case-sensitive

**Severity:** Medium

**Where:** `AuthService.get_user_by_identifier` ([auth/service.py:117](../app/auth/service.py#L117)) compares `User.email == identifier` exactly. Forgot password ([auth/service.py:268](../app/auth/service.py#L268)), invitations and email changes all compare emails in lower case.

**Steps:**

1. Have an account whose email is stored as `QA.Admin@example.com`. The first signup keeps the case it was typed in; invitations store lower case.
2. Sign in with `qa.admin@example.com`, or with the right email in any other case.

**Expected:** Signed in. Email addresses are case-insensitive everywhere else in the app.

**Actual:** `401 Incorrect username/email or password`. Only the exact stored spelling works. An invited user who types their address with a capital letter can't sign in, but forgot password still finds their account.

**Fix:** Compare `func.lower(User.email) == identifier.lower()`, and store the first admin's email in lower case like everyone else's. Usernames are also compared case-sensitively and `Jane` and `jane` can both exist; whether that is wanted is worth deciding at the same time.

### B4. `PATCH` project with `name: null` or `is_active: null` returns 500

**Severity:** Medium

**Where:** `ProjectService.update` ([projects/service.py:249](../app/monitoring/projects/service.py#L249)).

**Steps:**

```sh
curl -X PATCH "$API/api/v1/monitoring/projects/1" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"is_active": null}'
```

**Expected:** `422`, or the null ignored as the service already does for `slack_enabled` and the other switches.

**Actual:** `500 Internal Server Error`. The `NOT NULL` violation raises `IntegrityError`. After `rollback()` the handler builds its message from `project.name`, which is expired, so SQLAlchemy raises `MissingGreenlet`. A rename to a name that is already taken still answers `409` correctly, because `payload.name` is set then.

**Fix:** Drop explicit nulls for `name` and `is_active` (as the service already does for the mute switches), or reject them in `ProjectUpdate`. Read `project.name` before the rollback, never after.

### B5. `PATCH` website with `method: null` returns 500

**Severity:** Medium

**Where:** `WebsiteBase.known_method` ([websites/schemas.py:826](../app/monitoring/websites/schemas.py#L826)) calls `value.upper()`. `WebsiteUpdate` reuses it ([schemas.py:1072](../app/monitoring/websites/schemas.py#L1072)) on a field that may be `None` ([schemas.py:943](../app/monitoring/websites/schemas.py#L943)).

**Steps:**

```sh
curl -X PATCH "$API/api/v1/monitoring/websites/1" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"method": null}'
```

**Expected:** `422`, or the null ignored.

**Actual:** `500`: `AttributeError: 'NoneType' object has no attribute 'upper'`.

**Fix:** Return `None` early from the validator when `value is None`, then drop `method` in the service's `not_null` list (see B6).

### B6. `PATCH` website with `null` on a required field returns a false 409

**Severity:** Low

**Where:** `WebsiteService.update` strips explicit nulls for only five `NOT NULL` fields ([websites/service.py:555](../app/monitoring/websites/service.py#L555)). Any `IntegrityError` on commit is then reported as a duplicate URL ([service.py:626](../app/monitoring/websites/service.py#L626)).

**Steps:** `PATCH /api/v1/monitoring/websites/1` with any one of `{"name": null}`, `{"url": null}`, `{"check_interval_seconds": null}`, `{"max_down_alerts": null}`, `{"timeout_seconds": null}` or `{"is_enabled": null}`.

**Expected:** `422` naming the field, or the null ignored.

**Actual:** `409 https://example.com/ is already being monitored.` For `url: null` the message is `None is already being monitored.` The site is unchanged, but the error sends the user looking for a duplicate that doesn't exist.

**Fix:** Add these fields to the `not_null` list, or make them non-nullable in `WebsiteUpdate`. Only map an `IntegrityError` to `DuplicateWebsiteError` when it comes from the unique constraint.

The web UI never sends these nulls, so B4 to B6 only affect API clients and scripts. A fuzz of every `PATCH` and `PUT` body field with `null` (69 requests, everything except infrastructure) found no other 5xx.

### B7. Resuming a site paused while down sends a "recovered" alert for the whole pause

**Severity:** Low

**Where:** Pausing (`is_enabled: false`) leaves `status`, `down_since` and `down_alerts_sent` as they were. The first check after resuming takes the recovery branch of the state machine ([monitoring/service.py:333](../app/monitoring/service.py#L333), [service.py:350](../app/monitoring/service.py#L350)).

**Steps:**

1. A site is down.
2. Pause it, and leave it paused for 30 days.
3. Fix the site, resume it, and click **Check now**.

**Expected:** No alert, or one that doesn't claim the site was down while nobody was checking it.

**Actual:** A `recovered` alert goes to every channel, and the feed records `downtime_seconds` of 30 days. The paused time is counted as an outage.

**Fix:** Reset the outage state when a site is paused or resumed (`status = unknown`, `down_since = None`, `down_alerts_sent = 0`, thread ids cleared), or skip the recovery alert when the outage started before the latest resume.

### B8. A lost race when accepting an invitation returns 500

**Severity:** Low. Found by reading the code; not reproduced.

**Where:** `InvitationService.accept` ([invitations/service.py:354](../app/invitations/service.py#L354)).

**What happens:** If the insert loses a race on the email unique index, the handler rolls back and then reads `invitation.email`. That attribute is expired after the rollback, so it raises `MissingGreenlet` (as in B4) and the invitee gets `500` instead of `409`. Losing the race on the username is fine, because only `payload.username` is read then.

**Fix:** Read `invitation.email` into a local before the commit.

---

## Frontend

### F1. A failed `/users/me` on page load signs the user out

**Severity:** Medium

**Where:** [frontend/src/auth.jsx:30](../frontend/src/auth.jsx#L30): `api.me().then(setUser).catch(forget)`.

**Steps:**

1. Sign in.
2. Reload the page while the API can't be reached, or while the proxy returns `502` because the API is restarting.
3. Reload again once the API is back.

**Expected:** An error or retry while the API is down, and still signed in afterwards. The refresh cookie is still valid.

**Actual:** Sent to `/login`, with the access token deleted from `localStorage`. Without a token the client never tries the refresh cookie, so the user has to sign in again. A deploy or brief outage signs out everyone who reloads during it. Reproduced in Playwright with both a refused connection and a `502`.

**Fix:** Only `forget()` on `401` and `403`. A `401` is already handled by `request()`, which refreshes or calls the unauthorized handler. On a network error or `5xx`, keep the token and show a retry state.

### F2. Project counts and lists stop at the first page

**Severity:** Medium

**Where:** [Projects.jsx:219-222](../frontend/src/pages/Projects.jsx#L219-L222) builds every project's counts in the browser from one page of `listProjects()` (100), `listWebsites()` (100, the API's maximum) and `listResources({ limit: 200 })`. [ProjectDetail.jsx:42](../frontend/src/pages/ProjectDetail.jsx#L42) lists a project's sites from one page of 100. Neither page shows a "Showing X of Y" note, unlike Users, Invitations and Account requests.

**Steps:**

1. Have 106 websites: 1 in project A and 105 in project B.
2. Open **Projects**, then project B.

**Expected:** Project B shows 105 websites, and the Websites group shows 106 sites.

**Actual:**

- Project B's card shows **99 websites** and "0 of 99 up".
- The group header shows **100 sites**.
- Project B's page lists 100 rows with no hint that 5 are missing.
- A project whose sites all fall past the first 100 shows "No websites yet".
- Past 100 projects, the extra projects don't appear at all.

**Fix:** Take the per-project counts from the API, either a `project_id` on `/monitoring/websites/summary` or counts on the project list, rather than counting one page. Paginate the project page's site table as the Websites page already does.

### F3. Signing out in one tab leaves other tabs half signed in

**Severity:** Low

**Where:** [api.js:133](../frontend/src/api.js#L133) only calls the unauthorized handler when the failed request carried a token. Signing out in another tab has already removed the token from `localStorage`, so this tab's requests go out without one. Nothing listens for the `storage` event for the token, although `theme.js` and `prefs.js` do for their own keys.

**Steps:**

1. Open Watchly in two tabs.
2. Sign out in tab A.
3. In tab B, click **Projects**.

**Expected:** Tab B goes to the sign-in page.

**Actual:** Tab B stays in the app shell, still showing the user's name and the sidebar badge counts. Its API calls all return `401`. The Projects page shows **Not authenticated.** above "No projects yet. Create one to start monitoring websites." (see F4). This lasts until the user reloads.

**Fix:** Call the unauthorized handler on any `401` from a signed-in session, not only when a token was sent. Also listen for `storage` events on `watchly.accessToken` and sign the tab out when the token is removed.

### F4. The Projects page says "No projects yet" when loading failed

**Severity:** Low

**Where:** [Projects.jsx:287-305](../frontend/src/pages/Projects.jsx#L287-L305) renders the error banner and then the empty state, because `items` falls back to `[]` when `projects.data` is null.

**Steps:** Make `GET /monitoring/projects` fail, for example as in F3 or with the API down.

**Expected:** Only the error.

**Actual:** The error and "No projects yet. Create one to start monitoring websites.", which invites the user to create a project that may already exist.

**Fix:** Render the empty state only when the request succeeded (`projects.data` is set). Other list pages that use the `data?.items ?? []` pattern are worth checking for the same thing.

---

## Docs

### D1. `hld.md` and `reference.md` say a lockout suspends the account

**Where:**

- [doc/hld.md:666](hld.md#L666): "Too many wrong passwords suspend the account … set `is_active = false` and revoke its sessions".
- [doc/reference.md:390](reference.md#L390): "an account suspended for `MAX_FAILED_LOGIN_ATTEMPTS` … wrong passwords in a row".

**Actual behaviour:** Sign-in is locked for `LOGIN_LOCKOUT_MINUTES`. The account is not suspended and its sessions are not revoked (`AuthService.record_failed_login`). The lock lifts on its own, and reactivating clears it early. [security.md](security.md) describes this correctly.

**Fix:** Bring both passages in line with `security.md`.

---

## Not covered by this pass

- **AWS infrastructure monitoring.** The test instance ran with `INFRA_AWS_ENABLED` off, and there was no AWS account to point it at. The infrastructure routes and services were not exercised. The same `null` fuzz should be run there.
- **Docker agent pushes.** Host creation, the host page and `PATCH` on hosts and containers were exercised. A real agent push and the container alert rules were not.
- **Delivery.** No email, Slack, Telegram or WhatsApp was actually sent. Alerting was checked through the feed events and `alert_sent`.
- **Mobile layouts, browsers other than Chromium, and accessibility.**
