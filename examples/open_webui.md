# Open WebUI (or any OpenAI-compatible chat UI)

1. Start BankQL: `.venv/bin/uvicorn bankql.api:app --host 0.0.0.0 --port 8000`
2. Open WebUI → Settings → Connections → OpenAI API → add
   - URL: `http://<bankql-host>:8000/v1`
   - Key: one of `BANKQL_API_KEYS`
3. Pick model `bankql` (default source) or `bankql:bank_pg` etc. Follow-up questions use the chat history.
Replies contain a short summary, a markdown table (first 20 rows), code legends and the SQL used. Larger
results show an export id; download with `GET /v1/results/<id>.csv` and the same API key.
