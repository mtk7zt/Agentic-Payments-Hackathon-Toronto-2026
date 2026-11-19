#!/bin/bash
# Decrypt one dump and restore it. The default target is a new scratch database.
# --overwrite-production is required before this script will touch the live database.
# The age identity is passed on the command line and is not copied anywhere.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
  echo "usage: restore.sh --identity FILE [--dump PATH-or-gs-url] [--overwrite-production]" >&2
}

IDENTITY=""
DUMP=""
OVERWRITE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --identity)
      IDENTITY="${2:-}"
      shift 2
      ;;
    --dump)
      DUMP="${2:-}"
      shift 2
      ;;
    --overwrite-production)
      OVERWRITE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

refuse_stored_identity() {
  local real
  if [ -z "$IDENTITY" ] || [ ! -f "$IDENTITY" ]; then
    die "pass --identity FILE (the private key is not kept on the server)"
  fi
  real="$(readlink -f "$IDENTITY")"
  case "$real" in
    "$ROOT"/*)
      die "refusing an identity file inside the repo"
      ;;
    /etc/mandate/*)
      die "refusing an identity file under /etc/mandate"
      ;;
  esac
  local mode
  mode="$(stat -c %a "$real")"
  case "$mode" in
    ??[2367])
      echo "backup: warning: identity file is readable by other users" >&2
      ;;
  esac
}

resolve_dump() {
  if [ -n "$DUMP" ]; then
    if [ -f "$DUMP" ] || [[ "$DUMP" == gs://* ]]; then
      printf '%s\n' "$DUMP"
      return 0
    fi
    die "dump not found: $DUMP"
  fi
  if [ -n "${BACKUP_DIR:-}" ]; then
    local latest
    latest="$(find "$BACKUP_DIR/daily" -maxdepth 1 -type f -name 'mandate-*.dump.age' | sort | tail -n 1)"
    if [ -z "$latest" ]; then
      die "no dumps in $BACKUP_DIR/daily"
    fi
    printf '%s\n' "$latest"
    return 0
  fi
  if [ -z "${BACKUP_BUCKET:-}" ]; then
    die "set BACKUP_BUCKET, or pass --dump"
  fi
  command -v gcloud >/dev/null 2>&1 || die "gcloud is not installed"
  local latest
  latest="$(gcloud storage ls "gs://${BACKUP_BUCKET}/pg/daily/mandate-*.dump.age" | sort | tail -n 1)"
  if [ -z "$latest" ]; then
    die "no dumps in gs://${BACKUP_BUCKET}/pg/daily/"
  fi
  printf '%s\n' "$latest"
}

decrypt_to_stdout() {
  local source="$1"
  if [ -f "$source" ]; then
    age -d -i "$IDENTITY" < "$source"
    return 0
  fi
  gcloud storage cat "$source" | age -d -i "$IDENTITY"
}

main() {
  command -v age >/dev/null 2>&1 || die "age is not installed"
  command -v docker >/dev/null 2>&1 || die "docker is not installed"
  refuse_stored_identity
  local source db
  source="$(resolve_dump)"
  db="$(choose_database "$OVERWRITE")"
  assert_safe_database "$db" "$OVERWRITE"

  if [ "$OVERWRITE" = "1" ]; then
    echo "backup: stopping the gateway before replacing the live database"
    compose stop gateway
  else
    compose exec -T "$POSTGRES_SERVICE" \
      psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
      -c "CREATE DATABASE ${db} OWNER mandate_gateway"
  fi

  local -a restore_flags=(--no-owner --no-acl --exit-on-error)
  if [ "$OVERWRITE" = "1" ]; then
    restore_flags+=(--clean --if-exists)
  fi
  decrypt_to_stdout "$source" | compose exec -T "$POSTGRES_SERVICE" \
    pg_restore -U mandate_gateway -d "$db" "${restore_flags[@]}"

  run_migrate "$db"
  if [ "$OVERWRITE" = "1" ]; then
    compose up -d gateway
  fi
  run_ready "$db"
  echo "restore: database ${db}"
  echo "restore: source ${source}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
