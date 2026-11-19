#!/bin/sh
# Use DATABASE_URL from the environment when it is set (external Postgres).
# When it is empty, point at the bundled server as mandate_gateway.
set -eu

if [ -z "${DATABASE_URL:-}" ]; then
  if [ -z "${POSTGRES_PASSWORD:-}" ]; then
    echo "Set POSTGRES_PASSWORD, or set DATABASE_URL to an external database." >&2
    exit 1
  fi
  enc=$(python -c 'import os, urllib.parse; print(urllib.parse.quote(os.environ["POSTGRES_PASSWORD"], safe=""))')
  export DATABASE_URL="postgresql://mandate_gateway:${enc}@postgres:5432/mandate"
fi

# The read-only password is only for Postgres. The gateway does not need it.
unset MANDATE_READONLY_PASSWORD

if [ "$#" -eq 0 ]; then
  echo "with-database.sh: no command given" >&2
  exit 1
fi
exec "$@"
