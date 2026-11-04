#!/usr/bin/env bash
# Start everything for the local demo: Postgres, Redis (127.0.0.1 + password), both llama-server models
# (127.0.0.1 + --api-key) and the BankQL API; then print the demo URL. Ctrl-C stops what this script started.
#
#   scripts/demo.sh                 # GPU: both models resident (-ngl 99)
#   BANKQL_DEMO_MODE=cpu scripts/demo.sh   # CPU: 4 threads, 8k context, no GPU offload
# Needs: models (scripts/setup_local.sh or scripts/fetch_models.sh), llama-server on PATH, uv-synced .venv.
set -euo pipefail
cd "$(dirname "$0")/.."
MODE=${BANKQL_DEMO_MODE:-gpu}; PORT=${BANKQL_PORT:-8000}
mkdir -p data/logs
[ -f .env ] || cp .env.example .env

ensure_env() {  # add KEY=<random> to .env if missing (secrets never printed)
  grep -q "^$1=" .env || echo "$1=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" >> .env
}
ensure_env BANKQL_LLAMA_API_KEY
ensure_env BANKQL_REDIS_PASSWORD
grep -q "^BANKQL_API_KEYS=..*" .env && ! grep -q "^BANKQL_API_KEYS=change-me" .env || {
  sed -i.bak '/^BANKQL_API_KEYS=/d' .env && echo "BANKQL_API_KEYS=demo-$(python3 -c 'import secrets;print(secrets.token_hex(12))')" >> .env; }
. scripts/env.sh
export BANKQL_REDIS_URL="redis://:${BANKQL_REDIS_PASSWORD}@127.0.0.1:6379/0"

PIDS=()
cleanup() { [ ${#PIDS[@]} -gt 0 ] && kill "${PIDS[@]}" 2>/dev/null; scripts/serve_local.sh --stop >/dev/null 2>&1 || true
  redis-cli -a "$BANKQL_REDIS_PASSWORD" --no-auth-warning shutdown nosave >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

echo "== Postgres"
(service postgresql start || sudo service postgresql start || brew services start postgresql@16) >/dev/null 2>&1 || true
if grep -q "CHANGE_ME" .env || ! grep -q "^BANKQL_PG_URL=" .env; then sudo scripts/setup_postgres.sh
  unset BANKQL_PG_URL BANKQL_PG_ADMIN_URL BANKQL_BIRD_PG_URL BANKQL_BIRD_PG_ADMIN_URL; . scripts/env.sh; fi
[ -f data/fixture/financial.sqlite ] || .venv/bin/python scripts/make_fixture.py

echo "== Redis (127.0.0.1 only, password)"
redis-server --bind 127.0.0.1 --port 6379 --requirepass "$BANKQL_REDIS_PASSWORD" --protected-mode yes \
  --save "" --appendonly no --daemonize yes --logfile "$PWD/data/logs/redis.log"

echo "== llama-server ($MODE)"
if [ "$MODE" = cpu ]; then export BANKQL_NGL=0; fi
scripts/serve_local.sh --detach || echo "!! models not served — see data/logs/ (scripts/fetch_models.sh, BANKQL_ROUTER_GGUF)"

echo "== BankQL API"
.venv/bin/uvicorn bankql.api:app --host 127.0.0.1 --port "$PORT" > data/logs/api.log 2>&1 &
PIDS+=($!)
for _ in $(seq 1 60); do curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 1; done
echo
echo "Demo:     http://127.0.0.1:$PORT/demo"
echo "API key:  first value of BANKQL_API_KEYS in .env"
echo "Logs:     data/logs/   (Ctrl-C to stop)"
wait
