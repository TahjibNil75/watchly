# Contributing to Watchly

Thanks for helping. Bug reports, fixes, features and documentation are all
welcome. By taking part you agree to follow the [code of conduct](CODE_OF_CONDUCT.md).

## Reporting bugs and asking for features

[Open an issue](https://github.com/TahjibNil75/watchly/issues/new/choose) and
pick the bug report or feature request form. For a bug, the Watchly version
(`curl localhost:8000/health` shows it), how you run it, and the steps that
make it happen save a lot of back and forth.

**Security problems are the exception:** report them privately as described in
[SECURITY.md](SECURITY.md), never in a public issue.

## Setting up

You need Docker, Python 3.14 and Node.js 20.19 or later.
[`doc/local-setup.md`](doc/local-setup.md) walks through it step by step; in
short:

```bash
cp .env.example .env
docker compose up -d db                     # PostgreSQL only
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload               # API on http://127.0.0.1:8000/docs

cd frontend && npm install && npm run dev   # web app on http://localhost:5173
```

[`doc/hld.md`](doc/hld.md) explains how the pieces fit together, and
[`app/monitoring/websites/README.md`](app/monitoring/websites/README.md) how a
check becomes an alert.

## Making a change

1. **Branch from `main`**: `feat/short-name` for a feature, `fix/short-name`
   for a fix, `docs/…` or `chore/…` for the rest.
2. **Keep to the code around you.** Routes stay thin, rules live in the
   services, and comments say *why*.
3. **Database changes need a migration.** Change the model, then generate one
   and read it before committing:

   ```bash
   alembic revision --autogenerate -m "what changed"
   alembic upgrade head
   alembic check                # "No new upgrade operations detected."
   ```

   Keep `downgrade()` working: CI migrates all the way down and back up.
   Never edit a migration that has been released; add a new one.
4. **Run the backend tests**: `pip install -r requirements-dev.txt`, then
   `pytest`. They need the `db` container running, and use a database of
   their own, `watchly_test`, which they create and empty as they go.
5. **Check the frontend**: `cd frontend && npm run lint && npm run build`.
6. **Update the docs** your change touches, such as [`doc/apis.md`](doc/apis.md)
   for an endpoint, and add a line under **Unreleased** in
   [`CHANGELOG.md`](CHANGELOG.md) for anything a user would notice.

### Commit messages

Watchly uses [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add maintenance windows
fix: keep the Slack thread when the channel changes
docs: explain DNS pinning
```

`feat` and `fix` are what end up in the changelog. Use `docs`, `refactor`,
`chore`, `ci` or `test` for the rest, and add `!` (`feat!:`) with a
`BREAKING CHANGE:` footer when a change breaks the API, a setting or an
upgrade.

### Pull requests

Open the pull request against `main` and fill in the template. CI lints the
code, checks that migrations match the models and roll back cleanly, and builds
the web app and both Docker images; it must pass before merging. Small,
focused pull requests are reviewed fastest.

## Releasing (maintainers)

Watchly follows [semantic versioning](https://semver.org/): a fix is a patch
(1.0.1), a new feature a minor (1.1.0), and anything that breaks the API,
`.env` settings or upgrades a major (2.0.0).

1. On a `release/vX.Y.Z` branch, set the version in
   [`app/__init__.py`](app/__init__.py) and in the frontend with
   `cd frontend && npm version X.Y.Z --no-git-tag-version`.
2. In [`CHANGELOG.md`](CHANGELOG.md), rename **Unreleased** to
   `[X.Y.Z] - YYYY-MM-DD`, add a fresh empty **Unreleased** above it, and
   update the comparison links at the bottom.
3. Open a pull request, and merge it once CI passes.
4. Tag the merge commit and push the tag:

   ```bash
   git switch main && git pull
   git tag -a vX.Y.Z -m "Watchly vX.Y.Z"
   git push origin vX.Y.Z
   ```

   The [release workflow](.github/workflows/release.yml) then publishes a
   GitHub release whose notes are that version's changelog section.
