#!/bin/sh
# Print one version's section of CHANGELOG.md, without its heading: the body
# of that version's GitHub release. Fails when the version has no section.
set -eu

version=$1
notes=$(awk -v heading="## [$version]" '
  index($0, heading) == 1 { found = 1; next }
  found && (/^## \[/ || /^\[[^]]+\]: /) { exit }
  found { print }
' CHANGELOG.md | sed -e '/./,$!d')

if [ -z "$notes" ]; then
  echo "CHANGELOG.md has no section for $version." >&2
  exit 1
fi
printf '%s\n' "$notes"
