#!/bin/sh
# Fail unless app/__init__.py and frontend/package.json name the same version,
# and, given a tag such as v1.2.0, unless the tag names it too. Prints it.
set -eu

app=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' app/__init__.py)
web=$(node -p "require('./frontend/package.json').version")

if [ -z "$app" ] || [ "$app" != "$web" ]; then
  echo "Version mismatch: app/__init__.py has '$app', frontend/package.json '$web'." >&2
  exit 1
fi
if [ $# -gt 0 ] && [ "$1" != "v$app" ]; then
  echo "Tag '$1' does not match version '$app' in the code: tag v$app instead." >&2
  exit 1
fi
echo "$app"
