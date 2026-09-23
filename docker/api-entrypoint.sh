#!/bin/sh
# Bring the schema up to date and make sure the admin exists before serving.
# Both steps are idempotent, so restarting the container is always safe.
set -e

alembic upgrade head
python -m app.db.seed

exec "$@"
