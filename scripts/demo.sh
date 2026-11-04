#!/usr/bin/env bash
# Start everything for the local demo: Postgres, Redis (127.0.0.1 + password), both llama-server models
# (127.0.0.1 + --api-key) and the BankQL API; then print the demo URL. Ctrl-C stops what this script started.
#
#   scripts/demo.sh                 # GPU: both models resident (-ngl 99)
#   BANKQL_DEMO_MODE=cpu scripts/demo.sh   # CPU: 4 threads, 8k context, no GPU offload
# Needs: models in ./models (scripts/fetch_models.sh), llama-server on PATH (or .venv-llama), uv-synced .venv.
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
set -a; . ./.env; set +a
export BANKQL_REDIS_URL="redis://:${BANKQL_REDIS_PASSWORD}@127.0.0.1:6379/0"

PIDS=()
cleanup() { [ ${#PIDS[@]} -gt 0 ] && kill "${PIDS[@]}" 2>/dev/null; redis-cli -a "$BANKQL_REDIS_PASSWORD" --no-auth-warning shutdown nosave >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

echo "== Postgres"
(service postgresql start || sudo service postgresql start || brew services start postgresql@16) >/dev/null 2>&1 || true
if grep -q "CHANGE_ME" .env || ! grep -q "^BANKQL_PG_URL=" .env; then sudo scripts/setup_postgres.sh; set -a; . ./.env; set +a; fi
[ -f data/fixture/financial.sqlite ] || .venv/bin/python scripts/make_fixture.py

echo "== Redis (127.0.0.1 only, password)"
redis-server --bind 127.0.0.1 --port 6379 --requirepass "$BANKQL_REDIS_PASSWORD" --protected-mode yes \
  --save "" --appendonly no --daemonize yes --logfile "$PWD/data/logs/redis.log"

model() { .venv/bin/python -c "import yaml;m=yaml.safe_load(open('config/models.yaml'))['$1'];print('models/'+m['hf_file'])"; }
ROUTER_GGUF=$(model router); SQL_GGUF=$(model sql)
if [ "$MODE" = cpu ]; then GPU="-ngl 0 -t 4"; SQLCTX="-c 8192"; else GPU="-ngl 99"; SQLCTX="-c 24576 -np 3 --cache-type-k q8_0 --cache-type-v q8_0 -fa on"; fi
serve() {  # name port gguf extra-args
  [ -f "$3" ] || { echo "!! $3 missing — run scripts/fetch_models.sh"; return; }
  if command -v llama-server >/dev/null; then
    llama-server -m "$3" --host 127.0.0.1 --port "$2" --api-key "$BANKQL_LLAMA_API_KEY" $GPU $4 > "data/logs/$1.log" 2>&1 &
  else  # llama-cpp-python fallback (CPU build in .venv-llama)
    .venv-llama/bin/python -m llama_cpp.server --model "$3" --host 127.0.0.1 --port "$2" --api_key "$BANKQL_LLAMA_API_KEY" \
      --n_ctx 8192 --n_threads 4 > "data/logs/$1.log" 2>&1 &
  fi
  PIDS+=($!)
  for _ in $(seq 1 180); do curl -sf -H "Authorization: Bearer $BANKQL_LLAMA_API_KEY" "http://127.0.0.1:$2/v1/models" >/dev/null && { echo "   $1 ready on :$2"; return; }; sleep 2; done
  echo "!! $1 did not come up; see data/logs/$1.log"
}
echo "== llama-server ($MODE)"
serve router 8080 "$ROUTER_GGUF" "-c 8192"
serve sql 8081 "$SQL_GGUF" "$SQLCTX"   # note: -fa (flash attention) may be required for the quantized V cache

echo "== BankQL API"
.venv/bin/uvicorn bankql.api:app --host 127.0.0.1 --port "$PORT" > data/logs/api.log 2>&1 &
PIDS+=($!)
for _ in $(seq 1 60); do curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 1; done
echo
echo "Demo:     http://127.0.0.1:$PORT/demo"
echo "API key:  first value of BANKQL_API_KEYS in .env"
echo "Logs:     data/logs/   (Ctrl-C to stop)"
wait
