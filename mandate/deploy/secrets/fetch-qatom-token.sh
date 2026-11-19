#!/bin/bash
# Copy the Qatom token from Secret Manager onto the host path the gateway mounts.
# Uses the VM service account. No key file, and the token is never printed.
set -euo pipefail

secret="${QATOM_SECRET_NAME:-mandate-qatom-token}"
dest="${QATOM_TOKEN_HOST_PATH:-$(cd "$(dirname "$0")" && pwd)/qatom-token.json}"
# uid of the `mandate` user in the image (Dockerfile: useradd --uid 10001)
owner_uid="${QATOM_TOKEN_UID:-10001}"

die() {
  echo "qatom token: $*" >&2
  exit 1
}

command -v gcloud >/dev/null 2>&1 || die "gcloud is not installed"
command -v python3 >/dev/null 2>&1 || die "python3 is not installed"

tmp=""
cleanup() {
  if [ -n "$tmp" ] && [ -f "$tmp" ]; then
    rm -f "$tmp"
  fi
}
trap cleanup EXIT

umask 077
tmp="$(mktemp "${dest}.XXXXXX")"
gcloud secrets versions access latest --secret="$secret" > "$tmp"

python3 - "$tmp" <<'PY'
import json
import sys
path = sys.argv[1]
try:
    data = json.load(open(path, encoding="utf-8"))
except json.JSONDecodeError:
    sys.exit("secret is not JSON")
if not isinstance(data, dict) or not data.get("access_token"):
    sys.exit("secret has no access_token")
PY

chown "${owner_uid}:${owner_uid}" "$tmp"
chmod 600 "$tmp"
mv -f "$tmp" "$dest"
tmp=""
echo "qatom token: wrote ${dest} (mode 600, uid ${owner_uid})"
