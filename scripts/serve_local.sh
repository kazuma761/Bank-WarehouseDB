#!/usr/bin/env bash
# Both llama-servers on the GPU, bound to 127.0.0.1 and requiring --api-key:
#   Gemma (router, rewrite, summaries) on :8080;  Arctic-Text2SQL (SQL) on :8081 with 3 parallel slots + q8_0 KV cache.
#
#   scripts/serve_local.sh            # foreground; Ctrl-C stops both
#   scripts/serve_local.sh --detach   # background (logs in data/logs/, PIDs in data/logs/llama.pids)
#   scripts/serve_local.sh --stop
# Env: LLAMA_SERVER (binary, default llama-server on PATH), BANKQL_ROUTER_GGUF / BANKQL_SQL_GGUF (model paths,
# default models/<hf_file from config/models.yaml>), BANKQL_NGL (GPU layers, default 99),
# BANKQL_FA_FLAG (default "-fa on"; older llama.cpp builds want "-fa"; needed for the quantized V cache).
set -euo pipefail
cd "$(dirname "$0")/.."
PIDS_FILE=data/logs/llama.pids
mkdir -p data/logs

if [ "${1:-}" = "--stop" ]; then
  [ -f "$PIDS_FILE" ] && xargs -r kill < "$PIDS_FILE" 2>/dev/null || true
  rm -f "$PIDS_FILE"; echo "stopped"; exit 0
fi

[ -f .env ] || { echo "no .env — run scripts/setup_local.sh first"; exit 1; }
. scripts/env.sh
: "${BANKQL_LLAMA_API_KEY:?BANKQL_LLAMA_API_KEY missing in .env — run scripts/setup_local.sh}"
LLAMA=${LLAMA_SERVER:-llama-server}
command -v "$LLAMA" >/dev/null || { echo "llama-server not found; install llama.cpp (CUDA build) or set LLAMA_SERVER=/path/to/llama-server"; exit 1; }

model() { .venv/bin/python -c "import yaml;print('models/'+yaml.safe_load(open('config/models.yaml'))['$1']['hf_file'])"; }
ROUTER=${BANKQL_ROUTER_GGUF:-$(model router)}
SQL=${BANKQL_SQL_GGUF:-$(model sql)}
NGL=${BANKQL_NGL:-99}
FA=${BANKQL_FA_FLAG:--fa on}
for f in "$ROUTER" "$SQL"; do [ -f "$f" ] || { echo "model not found: $f"; exit 1; }; done

up() {  # name port
  for _ in $(seq 1 240); do
    curl -sf -H "Authorization: Bearer $BANKQL_LLAMA_API_KEY" "http://127.0.0.1:$2/v1/models" >/dev/null && { echo "  $1 ready on 127.0.0.1:$2"; return 0; }
    sleep 1
  done
  echo "!! $1 did not come up; see data/logs/$1.log"; return 1
}

: > "$PIDS_FILE"
# shellcheck disable=SC2086
setsid nohup "$LLAMA" -m "$ROUTER" --host 127.0.0.1 --port 8080 --api-key "$BANKQL_LLAMA_API_KEY" \
  -ngl "$NGL" -c 8192 > data/logs/router.log 2>&1 &
echo $! >> "$PIDS_FILE"
# shellcheck disable=SC2086
setsid nohup "$LLAMA" -m "$SQL" --host 127.0.0.1 --port 8081 --api-key "$BANKQL_LLAMA_API_KEY" \
  -ngl "$NGL" -c 24576 -np 3 --cache-type-k q8_0 --cache-type-v q8_0 $FA > data/logs/sql.log 2>&1 &
echo $! >> "$PIDS_FILE"
up router 8080 && up sql 8081 || { scripts/serve_local.sh --stop; exit 1; }

if [ "${1:-}" = "--detach" ]; then exit 0; fi
trap 'scripts/serve_local.sh --stop' INT TERM EXIT
echo "serving (Ctrl-C to stop)"
wait
