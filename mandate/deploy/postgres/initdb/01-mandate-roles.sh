#!/bin/bash
# Create the mandate database owned by mandate_gateway, plus a SELECT-only
# mandate_readonly login for a later read-only client (pgweb).
#
# Migrations run as mandate_gateway, so that role owns the tables. The
# PostgreSQL lockdown turns on row-level security with no policies and revokes
# public grants; the table owner still reads and writes, and other roles do
# not. mandate_readonly is not the owner, so without bypassing row-level
# security it would see no rows. It gets SELECT on tables mandate_gateway
# creates later, and no write grants.
set -euo pipefail

if [ -z "${MANDATE_DB_PASSWORD:-}" ] || [ -z "${MANDATE_READONLY_PASSWORD:-}" ]; then
  echo "MANDATE_DB_PASSWORD and MANDATE_READONLY_PASSWORD are required" >&2
  exit 1
fi

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-postgres}" --dbname postgres \
  -v gateway_password="$MANDATE_DB_PASSWORD" \
  -v readonly_password="$MANDATE_READONLY_PASSWORD" <<'SQL'
CREATE ROLE mandate_gateway WITH
  LOGIN
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT
  NOREPLICATION
  NOBYPASSRLS
  PASSWORD :'gateway_password';

CREATE ROLE mandate_readonly WITH
  LOGIN
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT
  NOREPLICATION
  BYPASSRLS
  PASSWORD :'readonly_password';

CREATE DATABASE mandate OWNER mandate_gateway;
SQL

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-postgres}" --dbname mandate <<'SQL'
REVOKE ALL ON DATABASE mandate FROM PUBLIC;
GRANT CONNECT, TEMP ON DATABASE mandate TO mandate_gateway;
GRANT CONNECT ON DATABASE mandate TO mandate_readonly;
GRANT USAGE, CREATE ON SCHEMA public TO mandate_gateway;
GRANT USAGE ON SCHEMA public TO mandate_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE mandate_gateway IN SCHEMA public
  GRANT SELECT ON TABLES TO mandate_readonly;
SQL
