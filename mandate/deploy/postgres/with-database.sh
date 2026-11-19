#!/bin/sh
# Use DATABASE_URL from the environment when it is set (external Postgres).
# When it is empty, point at the bundled server as mandate_gateway.
set -eu

if [ -z "${DATABASE_URL:-}" ]; then
  if [ -z "${MANDATE_DB_PASSWORD:-}" ]; then
    echo "Set MANDATE_DB_PASSWORD, or set DATABASE_URL to an external database." >&2
    exit 1
  fi
  enc=$(python -c 'import os, urllib.parse; print(urllib.parse.quote(os.environ["MANDATE_DB_PASSWORD"], safe=""))')
  export DATABASE_URL="postgresql://mandate_gateway:${enc}@postgres:5432/mandate"
fi

# The image superuser password and the read-only password stay on Postgres.
unset POSTGRES_PASSWORD MANDATE_READONLY_PASSWORD MANDATE_DB_PASSWORD

if [ "$#" -eq 0 ]; then
  echo "with-database.sh: no command given" >&2
  exit 1
fi
exec "$@"
