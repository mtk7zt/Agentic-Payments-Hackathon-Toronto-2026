#!/bin/bash
# Local check of dump, age, restore and migrate. Not the nightly job.
# Uses its own compose project and refuses to run while the real stack is up.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if docker ps --format '{{.Names}}' | grep -qx 'mandate-postgres-1'; then
  die "the mandate postgres container is already running; this check will not touch it"
fi
if [ -f "$ROOT/.env" ]; then
  die "refusing to replace an existing $ROOT/.env"
fi

WORKDIR="$(mktemp -d)"
CREATED_ENV=0
cleanup() {
  local status=$?
  if [ -n "${COMPOSE_PROJECT:-}" ]; then
    compose down -v >/dev/null 2>&1 || true
  fi
  rm -rf "$WORKDIR"
  if [ "$CREATED_ENV" = 1 ]; then
    rm -f "$ROOT/.env"
  fi
  exit "$status"
}
trap cleanup EXIT

# Name safety, before any container starts.
if bash -c "source '$ROOT/deploy/backup/lib.sh'; assert_safe_database mandate 0" >/dev/null 2>&1; then
  die "live database name was accepted"
fi
scratch_name="$(choose_database 0)"
case "$scratch_name" in
  mandate_restore_[0-9]*) ;;
  *) die "unexpected scratch name: $scratch_name" ;;
esac
assert_safe_database mandate 1

cat > "$ROOT/.env" <<'EOF'
MANDATE_MODE=sandbox
DATABASE_URL=
MANDATE_RAIL=mock
MANDATE_SELLER_MODE=mock
MANDATE_SELLER_TOKEN=seller-token-test
MANDATE_OWNER_TOKEN=owner-token-for-compose-check-32ch
MANDATE_CORS_ORIGINS=http://localhost:3000
MANDATE_DOMAIN=api.mandatepay.tech
POSTGRES_PASSWORD=super-only-secret
MANDATE_DB_PASSWORD=gateway-only-secret
MANDATE_READONLY_PASSWORD=ro-only-secret
EOF
CREATED_ENV=1
export ENV_FILE="$ROOT/.env"
export COMPOSE_PROJECT=mandatebk
export BACKUP_DIR="$WORKDIR/backups"
# A Sunday that is also the first, so daily, weekly and monthly copies are all written.
export BACKUP_NOW="2026-11-01 03:00:00"

compose up -d postgres
for _ in $(seq 1 40); do
  cid="$(postgres_cid || true)"
  if [ -n "$cid" ]; then
    state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || true)"
    if [ "$state" = "healthy" ]; then
      break
    fi
  fi
  sleep 3
done
if [ "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$(postgres_cid)")" != "healthy" ]; then
  compose logs postgres >&2 || true
  die "postgres did not become healthy"
fi

run_migrate mandate
compose exec -T postgres psql -U mandate_gateway -d mandate -v ON_ERROR_STOP=1 \
  -c "INSERT INTO mandate (id, agent_id, policy_json) VALUES ('bk_probe_1', 'roundtrip', '{}')"

age-keygen -o "$WORKDIR/identity.txt" >/dev/null 2>&1
AGE_RECIPIENT="$(sed -n 's/^# public key: //p' "$WORKDIR/identity.txt")"
export AGE_RECIPIENT
if [ -z "$AGE_RECIPIENT" ]; then
  die "age-keygen did not print a public key"
fi
"$ROOT/deploy/backup/backup.sh"

daily="$(find "$BACKUP_DIR/daily" -name 'mandate-*.dump.age' | sort | tail -n 1)"
weekly="$BACKUP_DIR/weekly/$(basename "$daily")"
monthly="$BACKUP_DIR/monthly/$(basename "$daily")"
[ -s "$daily" ] || die "daily ciphertext missing"
cmp "$daily" "$weekly"
cmp "$daily" "$monthly"
head -c 22 "$daily" | grep -q 'age-encryption.org/'
if grep -a -q 'bk_probe_1' "$daily"; then
  die "probe id was visible in the ciphertext"
fi

"$ROOT/deploy/backup/restore.sh" --identity "$WORKDIR/identity.txt" --dump "$daily" | tee "$WORKDIR/restore.out"
restored="$(sed -n 's/^restore: database //p' "$WORKDIR/restore.out")"
grep -q '"ready":true\|"ready": true' "$WORKDIR/restore.out"
found="$(compose exec -T postgres psql -U mandate_gateway -d "$restored" -tA \
  -c "SELECT id FROM mandate WHERE id = 'bk_probe_1'")"
[ "$found" = "bk_probe_1" ] || die "restored row was $found"
version="$(compose exec -T postgres psql -U mandate_gateway -d "$restored" -tA \
  -c "SELECT max(version) FROM schema_migrations")"
[ "$version" = "6" ] || die "schema version was $version"
echo "roundtrip: ok ${restored}"
