#!/bin/sh
# Bring the schema up to date before serving. Migrations are idempotent, so
# restarting the container is always safe. There is no admin to create: the
# first account to sign up becomes the admin.
set -e

alembic upgrade head

# Requests reach the API through the web container's nginx, so without this
# they would all seem to come from nginx and share one rate limit. Trust the
# X-Forwarded-For of the networks this container is on, where nginx is, unless
# FORWARDED_ALLOW_IPS already says whom to trust. A client connecting to the
# published API port from outside keeps its own address, so cannot forge one.
if [ -z "${FORWARDED_ALLOW_IPS:-}" ]; then
  FORWARDED_ALLOW_IPS=$(python - <<'EOF'
import ipaddress
import struct

trusted = ["127.0.0.1"]
try:
    with open("/proc/net/route") as routes:
        next(routes)
        for line in routes:
            fields = line.split()
            destination, gateway, mask = (int(fields[i], 16) for i in (1, 2, 7))
            # Directly attached networks: no gateway, and not the default route.
            if gateway == 0 and destination != 0:
                address = ipaddress.IPv4Address(struct.pack("=L", destination))
                trusted.append(str(ipaddress.IPv4Network((address, mask.bit_count()))))
except OSError:
    pass
print(",".join(dict.fromkeys(trusted)))
EOF
)
  export FORWARDED_ALLOW_IPS
fi

exec "$@"
