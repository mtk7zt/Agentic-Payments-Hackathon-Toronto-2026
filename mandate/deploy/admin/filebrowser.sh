#!/bin/sh
# First start creates the admin user from FILEBROWSER_PASSWORD. There is no default password.
set -eu

db=/database/filebrowser.db

if [ -z "${FILEBROWSER_PASSWORD:-}" ] || [ "$FILEBROWSER_PASSWORD" = "admin" ]; then
  echo "set FILEBROWSER_PASSWORD in ../.env to something other than admin" >&2
  exit 1
fi

if [ ! -s "$db" ]; then
  /filebrowser config init --database "$db"
  /filebrowser config set --database "$db" \
    --address 0.0.0.0 \
    --port 80 \
    --root /srv/mandate \
    --baseurl /files \
    --disable-thumbnails \
    --disable-exec \
    --auth.method json
  /filebrowser users add admin "$FILEBROWSER_PASSWORD" \
    --perm.admin --perm.execute=false --database "$db"
fi

exec /filebrowser --database "$db"
