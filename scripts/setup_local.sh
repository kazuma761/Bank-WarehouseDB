#!/usr/bin/env bash
# One-time local setup (Linux, GPU workstation):
#   Python env, secrets in .env, PostgreSQL roles + databases, synthetic fixture (tests), BIRD mini-dev "financial"
#   (original SQLite kept untouched; copies in DuckDB and Postgres), Arctic GGUF download, your Gemma GGUF,
#   DuckDB sqlite/postgres extensions for the pinned DuckDB, and a check of every source.
#
#   scripts/setup_local.sh --gemma /path/to/gemma-3-12b-it-Q4_K_M.gguf [--skip-bird] [--skip-models]
# Needs: uv, PostgreSQL 16 running locally (sudo for role setup), unzip, curl, internet.
set -euo pipefail
cd "$(dirname "$0")/.."
GEMMA="" SKIP_BIRD=0 SKIP_MODELS=0
while [ $# -gt 0 ]; do case "$1" in
  --gemma) GEMMA=$(realpath "$2"); shift 2 ;;
  --skip-bird) SKIP_BIRD=1; shift ;;
  --skip-models) SKIP_MODELS=1; shift ;;
  *) echo "unknown option $1"; exit 2 ;;
esac; done
BIRD_URL=https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip

step() { echo; echo "== $*"; }
set_env() { touch .env; grep -v "^$1=" .env > .env.tmp || true; echo "$1=$2" >> .env.tmp; mv .env.tmp .env; chmod 600 .env; }
rand() { python3 -c 'import secrets;print(secrets.token_urlsafe(24))'; }

step "Python environment (uv, pinned lock)"
uv sync --frozen
[ -f .env ] || cp .env.example .env
grep -q "^BANKQL_LLAMA_API_KEY=" .env || set_env BANKQL_LLAMA_API_KEY "$(rand)"
grep -q "^BANKQL_REDIS_PASSWORD=" .env || set_env BANKQL_REDIS_PASSWORD "$(rand)"
grep -q "^BANKQL_API_KEYS=change-me" .env && set_env BANKQL_API_KEYS "local-$(rand)"
. scripts/env.sh
set_env BANKQL_REDIS_URL "redis://:${BANKQL_REDIS_PASSWORD}@127.0.0.1:6379/0"

step "PostgreSQL: roles bankql_admin / bankql_ro, databases bankql_fixture + bankql_bird"
sudo scripts/setup_postgres.sh
sudo chown "$(id -u):$(id -g)" .env
unset BANKQL_PG_URL BANKQL_PG_ADMIN_URL BANKQL_BIRD_PG_URL BANKQL_BIRD_PG_ADMIN_URL  # setup_postgres.sh set new passwords
. scripts/env.sh

step "Synthetic fixture (used by the test suite)"
.venv/bin/python scripts/make_fixture.py

if [ "$SKIP_BIRD" = 0 ]; then
  step "BIRD mini-dev → data/bird (original SQLite untouched) → DuckDB + Postgres copies"
  mkdir -p data/bird
  [ -f data/bird/minidev.zip ] || curl -fL --retry 3 -o data/bird/minidev.zip "$BIRD_URL"
  unzip -q -o data/bird/minidev.zip -d data/bird
  .venv/bin/python scripts/load_bird.py --bird-dir data/bird
fi

if [ "$SKIP_MODELS" = 0 ]; then
  step "Models: Arctic-Text2SQL-R1-7B i1-IQ4_XS (download) + your Gemma GGUF"
  read -r repo file < <(.venv/bin/python -c "import yaml;m=yaml.safe_load(open('config/models.yaml'))['sql'];print(m['hf_repo'], m['hf_file'])")
  mkdir -p models
  [ -f "models/$file" ] || uvx --from "huggingface_hub[cli]" hf download "$repo" "$file" --local-dir models
  set_env BANKQL_SQL_GGUF "$PWD/models/$file"
  if [ -n "$GEMMA" ]; then
    [ -f "$GEMMA" ] || { echo "Gemma GGUF not found: $GEMMA"; exit 1; }
    set_env BANKQL_ROUTER_GGUF "$GEMMA"
  else
    echo "!! no --gemma given: set BANKQL_ROUTER_GGUF in .env to your Gemma 12B GGUF"
  fi
fi

step "DuckDB extensions for the pinned DuckDB (federation: sqlite_scanner, postgres_scanner)"
.venv/bin/python -c "
import duckdb
c = duckdb.connect()
for e in ('sqlite_scanner', 'postgres_scanner'):
    c.execute(f'INSTALL {e}'); c.execute(f'LOAD {e}')
print('duckdb', duckdb.__version__, 'extensions ok')"

step "Check every source (read-only is inspected, never tested by writing)"
for s in financial bank_demo bank_pg $( [ "$SKIP_BIRD" = 0 ] && echo bird_financial bird_demo bird_pg ); do
  echo "-- $s"; .venv/bin/bankql check-source "$s" || echo "!! $s has FAIL lines above"
done

echo
echo "Setup done. Next:  scripts/serve_local.sh --detach   then   scripts/eval_local.sh"
