"""D3: llama-server (127.0.0.1 + --api-key), Redis (localhost + password), compose port bindings."""
import re

import httpx

from bankql.llm import OpenAICompatClient
from tests.conftest import ROOT


def test_llm_client_sends_llama_api_key():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    c = OpenAICompatClient("http://127.0.0.1:8080/v1", api_key="llama-secret")
    c._http = httpx.Client(transport=httpx.MockTransport(handler), headers=c._http.headers)
    c.chat([{"role": "user", "content": "hi"}])
    assert seen["auth"] == "Bearer llama-secret"


def test_mercer_backend_gets_the_key(cfg, monkeypatch):
    from bankql.analytics import build_engine

    monkeypatch.setenv("BANKQL_LLAMA_API_KEY", "llama-secret")
    eng = build_engine(cfg)
    assert eng.backend._client.headers["authorization"] == "Bearer llama-secret"


def test_demo_scripts_bind_localhost_with_keys():
    sh = (ROOT / "scripts/demo.sh").read_text()
    ps = (ROOT / "scripts/demo.ps1").read_text()
    for text in (sh, ps):
        assert "--host" in text and "127.0.0.1" in text and "--api-key" in text
        assert "0.0.0.0" not in text
    assert re.search(r"redis-server --bind 127\.0\.0\.1 .*--requirepass", sh)
    assert "--host 127.0.0.1" in sh and ".venv/bin/uvicorn bankql.api:app --host 127.0.0.1" in sh


def test_compose_publishes_only_on_localhost():
    dc = (ROOT / "docker-compose.yml").read_text()
    for port in re.findall(r'ports: \[(.*?)\]', dc):
        assert port.strip('"').startswith(("127.0.0.1:", "${BANKQL_BIND:-127.0.0.1}:")), port
    assert "--requirepass" in dc and "BANKQL_LLAMA_API_KEY" in dc
