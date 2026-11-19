# Shared helpers for the Postgres backup and restore scripts.
# shellcheck shell=bash

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/deploy/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"
MANDATE_IMAGE="${MANDATE_IMAGE:-mandate:latest}"

die() {
  echo "backup: $*" >&2
  exit 1
}

compose() {
  local -a project=()
  if [ -n "${COMPOSE_PROJECT:-}" ]; then
    project=(-p "$COMPOSE_PROJECT")
  fi
  docker compose "${project[@]}" -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

toronto_date() {
  if [ -n "${BACKUP_NOW:-}" ]; then
    TZ=America/Toronto date -d "$BACKUP_NOW" "$@"
  else
    TZ=America/Toronto date "$@"
  fi
}

dump_name() {
  printf 'mandate-%s.dump.age' "$(toronto_date +%Y%m%dT%H%M%S%z)"
}

# Sunday, and the first of the month, both in America/Toronto.
copy_weekly() {
  [ "$(toronto_date +%u)" = "7" ]
}

copy_monthly() {
  [ "$(toronto_date +%d)" = "01" ]
}

# Prints age arguments for the recipient. AGE_RECIPIENT is an age1 public key
# or a path to a file with one age1 key per line.
age_recipient_args() {
  local value="${AGE_RECIPIENT:-}"
  if [ -z "$value" ]; then
    die "set AGE_RECIPIENT to an age1 public key or a recipients file"
  fi
  case "$value" in
    age1*)
      printf '%s\n' -r "$value"
      ;;
    *)
      if [ -f "$value" ]; then
        printf '%s\n' -R "$value"
      else
        die "AGE_RECIPIENT is not an age1 public key or an existing file"
      fi
      ;;
  esac
}

# overwrite 1 -> the live database. Anything else -> a new scratch name.
choose_database() {
  local overwrite="$1"
  if [ "$overwrite" = "1" ]; then
    printf '%s\n' mandate
    return 0
  fi
  printf 'mandate_restore_%s\n' "$(toronto_date +%Y%m%d%H%M%S)"
}

assert_safe_database() {
  local name="$1"
  local overwrite="$2"
  case "$name" in
    mandate)
      if [ "$overwrite" != "1" ]; then
        die "refusing to restore into mandate without --overwrite-production"
      fi
      ;;
    mandate_restore_[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9])
      ;;
    *)
      die "unsafe database name: $name"
      ;;
  esac
}

postgres_cid() {
  compose ps -q "$POSTGRES_SERVICE"
}

database_url() {
  local db="$1"
  local password enc
  password="$(
    python3 - "$ENV_FILE" <<'PY'
import sys
path = sys.argv[1]
value = ""
for line in open(path, encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, raw = line.split("=", 1)
    if key == "MANDATE_DB_PASSWORD":
        value = raw.strip().strip("\r")
print(value, end="")
PY
  )"
  if [ -z "$password" ]; then
    die "MANDATE_DB_PASSWORD is missing from $ENV_FILE"
  fi
  enc="$(MANDATE_DB_PASSWORD="$password" python3 -c 'import os, urllib.parse; print(urllib.parse.quote(os.environ["MANDATE_DB_PASSWORD"], safe=""))')"
  # The one-off container shares the database network namespace, so the host is loopback.
  printf 'postgresql://mandate_gateway:%s@127.0.0.1:5432/%s' "$enc" "$db"
}

run_app() {
  local url="$1"
  shift
  local cid token
  local -a token_mount=()
  cid="$(postgres_cid)"
  if [ -z "$cid" ]; then
    die "postgres container is not running"
  fi
  token="$ROOT/deploy/secrets/qatom-token.json"
  if [ -f "$token" ]; then
    token_mount=(-v "${token}:/home/mandate/.qatom/token.json:ro")
  fi
  docker run --rm \
    --network "container:${cid}" \
    --env-file "$ENV_FILE" \
    -e "DATABASE_URL=${url}" \
    -u mandate \
    -v "${ROOT}/deploy/postgres/with-database.sh:/home/mandate/with-database.sh:ro" \
    -v "${ROOT}/deploy/backup/ready_once.py:/opt/ready_once.py:ro" \
    "${token_mount[@]}" \
    --entrypoint /bin/sh \
    "$MANDATE_IMAGE" \
    /home/mandate/with-database.sh "$@"
}

run_migrate() {
  local db="$1"
  run_app "$(database_url "$db")" python -m backend.migrate
}

run_ready() {
  local db="$1"
  run_app "$(database_url "$db")" python /opt/ready_once.py
}
