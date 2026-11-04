#!/usr/bin/env bash
# REST API examples. Start the API:  .venv/bin/uvicorn bankql.api:app --port 8000
set -euo pipefail
API=${API:-http://localhost:8000}; KEY=${BANKQL_API_KEY:?export BANKQL_API_KEY=<one of BANKQL_API_KEYS>}
curl -s "$API/health"
curl -s -H "X-API-Key: $KEY" "$API/v1/sources"
curl -s -H "X-API-Key: $KEY" "$API/v1/sources/financial/tables" | head -c 400; echo
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' "$API/v1/ask" \
  -d '{"question": "How many loans are in debt?", "source": "financial", "session_id": "s1"}'
# Server-sent stage progress:
curl -N -H "X-API-Key: $KEY" -H 'content-type: application/json' "$API/v1/ask" \
  -d '{"question": "show client 12 cards", "stream": true}'
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' "$API/v1/sql/validate" \
  -d '{"source": "financial", "sql": "SELECT COUNT(*) FROM trans"}'
# Download a full result (export_id from an /v1/ask or /v1/sql/run response):
#   curl -H "X-API-Key: $KEY" -o result.csv "$API/v1/results/<export_id>.csv"
