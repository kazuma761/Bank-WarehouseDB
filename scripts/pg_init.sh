#!/bin/bash
# docker-entrypoint-initdb.d hook: same roles as scripts/setup_postgres.sh, passwords from the environment.
set -euo pipefail
psql -v ON_ERROR_STOP=1 -U postgres <<SQL
CREATE ROLE bankql_admin LOGIN PASSWORD '${BANKQL_PG_ADMIN_PASSWORD}';
CREATE ROLE bankql_ro LOGIN PASSWORD '${BANKQL_PG_RO_PASSWORD}';
CREATE DATABASE bankql_fixture OWNER bankql_admin;
CREATE DATABASE bankql_bird OWNER bankql_admin;
ALTER ROLE bankql_ro SET default_transaction_read_only = on;
ALTER ROLE bankql_ro SET statement_timeout = '5s';
SQL
for DB in bankql_fixture bankql_bird; do
psql -v ON_ERROR_STOP=1 -U postgres -d "$DB" <<SQL
REVOKE ALL ON DATABASE $DB FROM PUBLIC;
GRANT CONNECT ON DATABASE $DB TO bankql_ro, bankql_admin;
GRANT USAGE ON SCHEMA public TO bankql_ro;
GRANT ALL ON SCHEMA public TO bankql_admin;
ALTER DEFAULT PRIVILEGES FOR ROLE bankql_admin IN SCHEMA public GRANT SELECT ON TABLES TO bankql_ro;
SQL
done
