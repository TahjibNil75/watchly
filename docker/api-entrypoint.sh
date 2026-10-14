#!/bin/sh
# Bring the schema up to date before serving. Migrations are idempotent, so
# restarting the container is always safe. There is no admin to create: the
# first account to sign up becomes the admin.
set -e

alembic upgrade head

# Requests reach the API through the web container's Caddy, which puts the
# client's address in X-Forwarded-For. uvicorn believes that header only from
# the addresses in FORWARDED_ALLOW_IPS: docker-compose.yml sets it to the web
# container's pinned address. Unset, it defaults to 127.0.0.1, so a client that
# reaches the API directly cannot forge its address. Do not widen it to the
# Docker network: that includes the bridge gateway, which is how a client of a
# published port appears on Docker Desktop and through userland-proxy.

exec "$@"
