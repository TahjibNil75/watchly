#!/bin/sh
# Bring the schema up to date before serving. Migrations are idempotent, so
# restarting the container is always safe. There is no admin to create: the
# first account to sign up becomes the admin.
set -e

alembic upgrade head

exec "$@"
