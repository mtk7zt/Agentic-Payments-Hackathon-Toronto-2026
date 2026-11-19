#!/bin/bash
# Dump the mandate database, encrypt it with age, and upload the ciphertext.
# The custom-format dump is only ever in the pipe. A failed run leaves no file behind.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PARTIAL=""
cleanup_partial() {
  if [ -n "$PARTIAL" ] && [ -f "$PARTIAL" ]; then
    rm -f "$PARTIAL"
  fi
}
trap cleanup_partial ERR

encrypt_dump() {
  local -a recipient=()
  mapfile -t recipient < <(age_recipient_args || true)
  if [ "${#recipient[@]}" -lt 2 ]; then
    die "set AGE_RECIPIENT to an age1 public key or a recipients file"
  fi
  compose exec -T "$POSTGRES_SERVICE" \
    pg_dump -U mandate_gateway -d mandate -Fc | age "${recipient[@]}"
}

retain_local() {
  local name="$1"
  local src="$BACKUP_DIR/daily/$name"
  if copy_weekly; then
    mkdir -p "$BACKUP_DIR/weekly"
    cp "$src" "$BACKUP_DIR/weekly/$name"
  fi
  if copy_monthly; then
    mkdir -p "$BACKUP_DIR/monthly"
    cp "$src" "$BACKUP_DIR/monthly/$name"
  fi
}

retain_gcs() {
  local name="$1"
  local daily="gs://${BACKUP_BUCKET}/pg/daily/${name}"
  if copy_weekly; then
    gcloud storage cp "$daily" "gs://${BACKUP_BUCKET}/pg/weekly/${name}"
  fi
  if copy_monthly; then
    gcloud storage cp "$daily" "gs://${BACKUP_BUCKET}/pg/monthly/${name}"
  fi
}

main() {
  command -v age >/dev/null 2>&1 || die "age is not installed"
  command -v docker >/dev/null 2>&1 || die "docker is not installed"
  local name
  name="$(dump_name)"

  if [ -n "${BACKUP_DIR:-}" ]; then
    mkdir -p "$BACKUP_DIR/daily"
    PARTIAL="$BACKUP_DIR/daily/$name"
    encrypt_dump > "$PARTIAL"
    if [ ! -s "$PARTIAL" ]; then
      die "encrypted dump was empty"
    fi
    PARTIAL=""
    retain_local "$name"
    echo "backup: wrote ${BACKUP_DIR}/daily/${name}"
    return 0
  fi

  if [ -z "${BACKUP_BUCKET:-}" ]; then
    die "set BACKUP_BUCKET, or BACKUP_DIR for a local check"
  fi
  command -v gcloud >/dev/null 2>&1 || die "gcloud is not installed"
  encrypt_dump | gcloud storage cp - "gs://${BACKUP_BUCKET}/pg/daily/${name}"
  retain_gcs "$name"
  echo "backup: wrote gs://${BACKUP_BUCKET}/pg/daily/${name}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
