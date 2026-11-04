"""Talk to BankQL as if it were an OpenAI chat model (model name 'bankql' or 'bankql:<source>').
Any OpenAI-API client works; this uses plain httpx so no cloud SDK is involved."""
import os

import httpx

r = httpx.post("http://localhost:8000/v1/chat/completions", timeout=600,
               headers={"Authorization": f"Bearer {os.environ['BANKQL_API_KEY']}"},
               json={"model": "bankql:financial", "messages": [{"role": "user", "content": "How many loans are in debt?"}]})
print(r.json()["choices"][0]["message"]["content"])  # summary + markdown table + ```sql block
