#!/usr/bin/env bash
# Full local evaluation with both models resident; everything goes to eval/results/ and REPORT.md is regenerated.
# Every runner is resumable: re-run this script after an interruption and finished items are skipped.
#
#   scripts/eval_local.sh                      # all steps
#   scripts/eval_local.sh lookup analytics     # a subset: lookup | analytics | chat | latency | report
set -euo pipefail
cd "$(dirname "$0")/.."
STEPS=${*:-lookup analytics chat latency report}
. scripts/env.sh
PY=.venv/bin/python
KEY=${BANKQL_API_KEYS%%,*}
STARTED_LLAMA=0 API_PID=""
cleanup() { [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null || true; [ "$STARTED_LLAMA" = 1 ] && scripts/serve_local.sh --stop || true; }
trap cleanup EXIT

# Redis (Mercer schema cache): localhost only, password from .env
redis-cli -a "$BANKQL_REDIS_PASSWORD" --no-auth-warning ping >/dev/null 2>&1 || \
  redis-server --bind 127.0.0.1 --port 6379 --requirepass "$BANKQL_REDIS_PASSWORD" --save "" --appendonly no --daemonize yes
# Models: reuse running servers or start them for this run
if ! curl -sf -H "Authorization: Bearer $BANKQL_LLAMA_API_KEY" http://127.0.0.1:8081/v1/models >/dev/null; then
  scripts/serve_local.sh --detach; STARTED_LLAMA=1
fi
nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader > eval/results/gpu.txt 2>/dev/null || true

for step in $STEPS; do case "$step" in
  lookup)
    echo "== Phase 3: lookup eval (router = Gemma) on bird_demo"
    [ -f eval/datasets/lookup_bird.jsonl ] || $PY eval/make_lookup_set.py --source bird_demo --out eval/datasets/lookup_bird.jsonl
    $PY eval/run_lookup.py --items eval/datasets/lookup_bird.jsonl --source bird_demo --out eval/results/lookup.jsonl ;;
  analytics)
    echo "== Phase 4: BIRD financial — fast and full mode, with and without evidence"
    $PY eval/run_analytics.py --bird-dir data/bird --mode fast --evidence both
    $PY eval/run_analytics.py --bird-dir data/bird --mode full --evidence both ;;
  chat)
    echo "== Chat smoke test through /v1/ask with the real models"
    .venv/bin/uvicorn bankql.api:app --host 127.0.0.1 --port 8000 > data/logs/api_eval.log 2>&1 &
    API_PID=$!
    for _ in $(seq 1 60); do curl -sf http://127.0.0.1:8000/health >/dev/null && break; sleep 1; done
    $PY eval/chat_smoke.py --url http://127.0.0.1:8000 --key "$KEY" --source bird_demo
    kill "$API_PID"; API_PID="" ;;
  latency)
    echo "== Latency: pipeline (FakeLLM) + real models (prefill / decode per call)"
    $PY eval/latency.py --reps 3 > /dev/null
    $PY eval/latency_real.py eval/results/chat_smoke_real.jsonl > /dev/null ;;
  report)
    echo "== REPORT.md"
    $PY eval/make_report.py ;;
  *) echo "unknown step $step"; exit 2 ;;
esac; done
echo "Results in eval/results/; see REPORT.md (fill in the spot-check table by hand)."
