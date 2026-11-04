#!/usr/bin/env bash
# Creates the fixture DB + roles on a local PostgreSQL 16 and writes passwords to .env (never printed).
# Roles: bankql_admin (loads data, owner) and bankql_ro (SELECT only, read-only transactions, 5s timeout).
set -euo pipefail
cd "$(dirname "$0")/.."
service postgresql start >/dev/null 2>&1 || true
PW_ADMIN=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))"); PW_RO=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))")
su postgres -c "psql -v ON_ERROR_STOP=1 -q" <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bankql_admin') THEN CREATE ROLE bankql_admin LOGIN PASSWORD '$PW_ADMIN'; ELSE ALTER ROLE bankql_admin PASSWORD '$PW_ADMIN'; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bankql_ro') THEN CREATE ROLE bankql_ro LOGIN PASSWORD '$PW_RO'; ELSE ALTER ROLE bankql_ro PASSWORD '$PW_RO'; END IF;
END \$\$;
SELECT 'CREATE DATABASE bankql_fixture OWNER bankql_admin' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='bankql_fixture')\gexec
ALTER ROLE bankql_ro SET default_transaction_read_only = on;
ALTER ROLE bankql_ro SET statement_timeout = '5s';
SQL
su postgres -c "psql -v ON_ERROR_STOP=1 -q -d bankql_fixture" <<'SQL'
REVOKE ALL ON DATABASE bankql_fixture FROM PUBLIC;
GRANT CONNECT ON DATABASE bankql_fixture TO bankql_ro, bankql_admin;
GRANT USAGE ON SCHEMA public TO bankql_ro;
GRANT ALL ON SCHEMA public TO bankql_admin;
ALTER DEFAULT PRIVILEGES FOR ROLE bankql_admin IN SCHEMA public GRANT SELECT ON TABLES TO bankql_ro;
SQL
grep -v '^BANKQL_PG' .env 2>/dev/null > .env.tmp || true
cat >> .env.tmp <<ENV
BANKQL_PG_ADMIN_URL=postgresql+psycopg://bankql_admin:$PW_ADMIN@localhost:5432/bankql_fixture
BANKQL_PG_URL=postgresql+psycopg://bankql_ro:$PW_RO@localhost:5432/bankql_fixture
ENV
mv .env.tmp .env; chmod 600 .env
echo "postgres ready; credentials in .env"
