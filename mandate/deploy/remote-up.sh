#!/bin/bash
# Bring up the default Compose stack and wait until /ready is green.
# Called on the server after the deploy checkout. No extra profiles.
set -euo pipefail

cd "$(dirname "$0")"

if docker info >/dev/null 2>&1; then
  compose=(docker compose --env-file ../.env)
else
  compose=(sudo docker compose --env-file ../.env)
fi

if [ -n "${GCP_AR_HOST:-}" ]; then
  gcloud auth configure-docker "$GCP_AR_HOST" --quiet
fi

"${compose[@]}" up -d --build

for _ in $(seq 1 36); do
  if "${compose[@]}" exec -T gateway python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=4)'; then
    echo "ready"
    exit 0
  fi
  sleep 5
done

echo "gateway /ready did not succeed" >&2
"${compose[@]}" logs --tail=40 gateway >&2 || true
exit 1
