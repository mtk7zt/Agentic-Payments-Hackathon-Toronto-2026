#!/bin/bash
# Runs on every boot of mandate-prod, from instance metadata.
# Formats the data disk once, mounts /srv/mandate, installs Docker and git,
# clones the repo if it is missing, and writes .env from Secret Manager.
# Secret values are never printed. A value of placeholder-change-me is skipped.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-mandate-515200}"
REPO_URL="${REPO_URL:-https://github.com/mtk7zt/Agentic-Payments-Hackathon-Toronto-2026.git}"
APP_DIR="${APP_DIR:-/srv/mandate/app}"
DATA_DEVICE="${DATA_DEVICE:-/dev/disk/by-id/google-mandate-data}"
METADATA_URL="${MANDATE_METADATA_URL:-http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token}"
SECRET_API="${MANDATE_SECRET_API:-https://secretmanager.googleapis.com/v1}"
ENV_PATH="${MANDATE_ENV_PATH:-${APP_DIR}/mandate/.env}"
TOKEN_PATH="${MANDATE_TOKEN_PATH:-${APP_DIR}/mandate/deploy/secrets/qatom-token.json}"
BACKUP_ENV_PATH="${MANDATE_BACKUP_ENV_PATH:-/etc/mandate/backup.env}"
BACKUP_BUCKET="${BACKUP_BUCKET:-mandate-515200-mandate-backups}"
PLACEHOLDER="placeholder-change-me"

log() {
  echo "startup: $*" >&2
}

# Prints one status word: value, placeholder, missing, or error.
# On value, the secret text is written to the outfile and nowhere else.
fetch_secret() {
  set +x
  local name="$1"
  local out="$2"
  local tmp code rc
  tmp="$(mktemp)"
  code="$(curl -sS -o "$tmp" -w '%{http_code}' \
    -H "Authorization: Bearer ${token}" \
    "${SECRET_API}/projects/${PROJECT_ID}/secrets/${name}/versions/latest:access" || true)"
  if [ "$code" = "404" ]; then
    rm -f "$tmp"
    echo missing
    return 0
  fi
  if [ "$code" != "200" ]; then
    rm -f "$tmp"
    log "cannot read ${name} (HTTP ${code})"
    echo error
    return 0
  fi
  rc=0
  python3 - "$tmp" "$out" "$PLACEHOLDER" <<'PY' || rc=$?
import base64
import json
import sys

src, dest, placeholder = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    raw = json.loads(open(src, encoding="utf-8").read())
    text = base64.b64decode(raw["payload"]["data"]).decode()
except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError):
    sys.exit(2)
text = text.strip("\r\n")
if text == placeholder:
    sys.exit(10)
with open(dest, "w", encoding="utf-8") as fh:
    fh.write(text)
PY
  rm -f "$tmp"
  if [ "$rc" -eq 0 ]; then
    chmod 600 "$out"
    echo value
    return 0
  fi
  if [ "$rc" -eq 10 ]; then
    echo placeholder
    return 0
  fi
  log "cannot read ${name}"
  echo error
  return 0
}

read_token() {
  set +x
  local body
  body="$(curl -fsS -H "Metadata-Flavor: Google" "$METADATA_URL")" || {
    log "metadata token request failed"
    exit 1
  }
  token="$(printf '%s' "$body" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    tok = data.get("access_token") if isinstance(data, dict) else None
except (json.JSONDecodeError, ValueError):
    sys.exit("startup: metadata response was not usable")
if not isinstance(tok, str) or not tok:
    sys.exit("startup: metadata response has no access_token")
sys.stdout.write(tok)
')"
  body=""
}

write_env_file() {
  python3 - "$1" "$2" <<'PY'
import os
import sys

stage, dest = sys.argv[1], sys.argv[2]
lines = []
for name in sorted(os.listdir(stage)):
    value = open(os.path.join(stage, name), encoding="utf-8").read()
    if "\n" in value or "\r" in value:
        sys.exit(f"startup: {name} contains a newline")
    if value == "" or any(c in value for c in " \t#'\"\\$"):
        lines.append(name + "='" + value.replace("'", "'\"'\"'") + "'")
    else:
        lines.append(name + "=" + value)
text = "\n".join(lines) + "\n"
tmp = dest + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(text)
os.chmod(tmp, 0o600)
os.replace(tmp, dest)
PY
}

render_env() {
  local token=""
  local stage secret_file status key
  command -v curl >/dev/null 2>&1 || { log "curl is not installed"; exit 1; }
  command -v python3 >/dev/null 2>&1 || { log "python3 is not installed"; exit 1; }

  read_token
  stage="$(mktemp -d)"
  # Plain settings. These are not secrets.
  printf '%s' 'production' > "${stage}/MANDATE_MODE"
  printf '%s' 'qatom' > "${stage}/MANDATE_RAIL"
  printf '%s' 'http' > "${stage}/MANDATE_SELLER_MODE"
  printf '%s' 'http://seller:8001' > "${stage}/MANDATE_SELLER_URL"
  printf '%s' 'https://app.mandatepay.tech' > "${stage}/MANDATE_CORS_ORIGINS"
  printf '%s' 'api.mandatepay.tech' > "${stage}/MANDATE_DOMAIN"
  printf '%s' '/srv/mandate/postgres' > "${stage}/MANDATE_PGDATA"
  printf '%s' 'https://mcp.m.todaq.net/mcp' > "${stage}/QATOM_MCP_URL"
  printf '%s' '{}' > "${stage}/QATOM_ITEM_MAP"

  # Secret Manager name, then the env name compose and .env.example already use.
  # mandate-db-password and mandate-readonly-password are not in the bootstrap.
  # shellcheck disable=SC2312
  while IFS=' ' read -r secret key; do
    [ -n "$secret" ] || continue
    secret_file="$(mktemp)"
    status="$(fetch_secret "$secret" "$secret_file")"
    case "$status" in
      value)
        cp "$secret_file" "${stage}/${key}"
        chmod 600 "${stage}/${key}"
        ;;
      placeholder)
        log "skip ${secret} (still a placeholder)"
        if [ "$key" = "DATABASE_URL" ]; then
          : > "${stage}/DATABASE_URL"
        fi
        ;;
      missing)
        log "skip ${secret} (not in Secret Manager)"
        if [ "$key" = "DATABASE_URL" ]; then
          : > "${stage}/DATABASE_URL"
        fi
        ;;
      error)
        rm -f "$secret_file"
        rm -rf "$stage"
        token=""
        log "left the existing env file in place"
        exit 1
        ;;
      *)
        rm -f "$secret_file"
        rm -rf "$stage"
        token=""
        log "left the existing env file in place"
        exit 1
        ;;
    esac
    rm -f "$secret_file"
  done <<'MAP'
mandate-database-url DATABASE_URL
mandate-postgres-password POSTGRES_PASSWORD
mandate-db-password MANDATE_DB_PASSWORD
mandate-readonly-password MANDATE_READONLY_PASSWORD
mandate-owner-token MANDATE_OWNER_TOKEN
mandate-checkpoint-key MANDATE_CHECKPOINT_KEY
mandate-seller-token MANDATE_SELLER_TOKEN
mandate-goat-flow-api-key GOAT_FLOW_API_KEY
MAP

  mkdir -p "$(dirname "$ENV_PATH")"
  write_env_file "$stage" "$ENV_PATH"
  if [ "$(id -u)" -eq 0 ]; then
    chown root:root "$ENV_PATH"
  fi
  chmod 600 "$ENV_PATH"
  rm -rf "$stage"
  log "wrote ${ENV_PATH} (mode 600)"

  # Qatom stays a file. Compose mounts this path; it is not an env value.
  secret_file="$(mktemp)"
  status="$(fetch_secret mandate-qatom-token "$secret_file")"
  case "$status" in
    value)
      if python3 - "$secret_file" <<'PY'
import json
import sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except json.JSONDecodeError:
    sys.exit(1)
if not isinstance(data, dict) or not data.get("access_token"):
    sys.exit(1)
PY
      then
        mkdir -p "$(dirname "$TOKEN_PATH")"
        cp "$secret_file" "$TOKEN_PATH"
        chmod 600 "$TOKEN_PATH"
        if [ "$(id -u)" -eq 0 ]; then
          chown 10001:10001 "$TOKEN_PATH"
        fi
        log "wrote qatom token file (mode 600)"
      else
        log "skip mandate-qatom-token (not a token file)"
      fi
      ;;
    placeholder) log "skip mandate-qatom-token (still a placeholder)" ;;
    missing) log "skip mandate-qatom-token (not in Secret Manager)" ;;
    error)
      rm -f "$secret_file"
      token=""
      exit 1
      ;;
  esac
  rm -f "$secret_file"

  # The backup file gets the age1 public key only. A private key is refused.
  secret_file="$(mktemp)"
  status="$(fetch_secret mandate-backup-encryption-key "$secret_file")"
  local recipient_file=""
  case "$status" in
    value)
      local kind
      kind="$(python3 - "$secret_file" <<'PY'
import sys
text = open(sys.argv[1], encoding="utf-8").read().strip()
if text.startswith("AGE-SECRET-KEY-"):
    print("private")
elif text.startswith("age1"):
    print("public")
else:
    print("other")
PY
)"
      case "$kind" in
        public) recipient_file="$secret_file" ;;
        private) log "refuse mandate-backup-encryption-key (private key stays off the VM)" ;;
        *) log "skip mandate-backup-encryption-key (not an age1 public key)" ;;
      esac
      ;;
    placeholder) log "skip mandate-backup-encryption-key (still a placeholder)" ;;
    missing) log "skip mandate-backup-encryption-key (not in Secret Manager)" ;;
    error)
      rm -f "$secret_file"
      token=""
      exit 1
      ;;
  esac

  mkdir -p "$(dirname "$BACKUP_ENV_PATH")"
  if [ "$(dirname "$BACKUP_ENV_PATH")" = "/etc/mandate" ]; then
    chmod 700 /etc/mandate
  fi
  python3 - "$BACKUP_ENV_PATH" "$BACKUP_BUCKET" "${recipient_file}" <<'PY'
import os
import sys

dest, bucket, recipient_path = sys.argv[1], sys.argv[2], sys.argv[3]
lines = ["BACKUP_BUCKET=" + bucket]
if recipient_path:
    recipient = open(recipient_path, encoding="utf-8").read().strip()
    if "\n" not in recipient and "\r" not in recipient:
        lines.append("AGE_RECIPIENT=" + recipient)
text = "\n".join(lines) + "\n"
tmp = dest + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(text)
os.chmod(tmp, 0o600)
os.replace(tmp, dest)
PY
  if [ "$(id -u)" -eq 0 ]; then
    chown root:root "$BACKUP_ENV_PATH"
  fi
  chmod 600 "$BACKUP_ENV_PATH"
  rm -f "$secret_file"
  token=""
  log "wrote ${BACKUP_ENV_PATH} (mode 600)"
}

prepare_disk() {
  local disk="$DATA_DEVICE"
  local fs uuid
  for _ in $(seq 1 30); do
    [ -b "$disk" ] && break
    sleep 2
  done
  if [ ! -b "$disk" ]; then
    log "data disk ${disk} did not appear"
    exit 1
  fi
  fs="$(blkid -o value -s TYPE "$disk" || true)"
  if [ -z "$fs" ]; then
    log "formatting ${disk}"
    mkfs.ext4 -F -m 0 -E lazy_itable_init=0,lazy_journal_init=0,discard "$disk"
  fi
  uuid="$(blkid -o value -s UUID "$disk")"
  if ! grep -q '[[:space:]]/srv/mandate[[:space:]]' /etc/fstab; then
    printf 'UUID=%s /srv/mandate ext4 defaults,nofail,discard 0 2\n' "$uuid" >> /etc/fstab
  fi
  mkdir -p /srv/mandate
  if ! mountpoint -q /srv/mandate; then
    mount /srv/mandate
  fi

  if [ ! -f /srv/mandate/swapfile ]; then
    if ! fallocate -l 2G /srv/mandate/swapfile; then
      dd if=/dev/zero of=/srv/mandate/swapfile bs=1M count=2048
    fi
    chmod 600 /srv/mandate/swapfile
    mkswap /srv/mandate/swapfile
  fi
  if ! grep -q '/srv/mandate/swapfile' /etc/fstab; then
    echo '/srv/mandate/swapfile none swap sw 0 0' >> /etc/fstab
  fi
  if ! swapon --show | grep -q '/srv/mandate/swapfile'; then
    swapon /srv/mandate/swapfile
  fi
}

install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  if ! command -v git >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
    apt-get update
    apt-get install -y ca-certificates curl git python3
  fi
  if ! docker compose version >/dev/null 2>&1; then
    apt-get update
    apt-get install -y ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    if [ ! -f /etc/apt/keyrings/docker.asc ]; then
      curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
      chmod a+r /etc/apt/keyrings/docker.asc
    fi
    # shellcheck disable=SC1091
    . /etc/os-release
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' \
      "$(dpkg --print-architecture)" "${VERSION_CODENAME}" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  fi
  systemctl enable --now docker
}

prepare_checkout() {
  local drop tmp
  groupadd --force google-sudoers
  if [ ! -d "${APP_DIR}/.git" ]; then
    git clone "$REPO_URL" "$APP_DIR"
  fi
  # OS Login does not add the deploy account to google-sudoers, so that user
  # cannot write this checkout. The deploy workflow runs the remote step with
  # sudo. safe.directory is system-wide so root can use git here.
  chgrp -R google-sudoers "$APP_DIR"
  chmod -R g+rwX "$APP_DIR"
  find "$APP_DIR" -type d -exec chmod g+s {} +
  if ! git config --system --get-all safe.directory 2>/dev/null | grep -Fxq "$APP_DIR"; then
    git config --system --add safe.directory "$APP_DIR"
  fi
  drop="/etc/sudoers.d/google-sudoers-mandate"
  tmp="$(mktemp)"
  printf '%s\n' '%google-sudoers ALL=(ALL) NOPASSWD:ALL' > "$tmp"
  visudo -cf "$tmp"
  install -m 440 "$tmp" "$drop"
  rm -f "$tmp"

  mkdir -p /srv/mandate/postgres
  chown 999:999 /srv/mandate/postgres
  chmod 700 /srv/mandate/postgres
  # The group change above would make an existing .env group-readable.
  lock_secrets
}

lock_secrets() {
  if [ -f "$ENV_PATH" ]; then
    chmod 600 "$ENV_PATH"
    chown root:root "$ENV_PATH"
  fi
  if [ -f "$TOKEN_PATH" ]; then
    chmod 600 "$TOKEN_PATH"
    chown 10001:10001 "$TOKEN_PATH"
  fi
  if [ -f "$BACKUP_ENV_PATH" ]; then
    chmod 600 "$BACKUP_ENV_PATH"
    chown root:root "$BACKUP_ENV_PATH"
  fi
}

if [ "${STARTUP_RENDER_ONLY:-}" = "1" ]; then
  render_env
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  log "run as root"
  exit 1
fi

prepare_disk
install_packages
prepare_checkout
render_env
