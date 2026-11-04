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


def test_local_scripts_bind_localhost_with_keys():
    """llama-server is started only by serve_local.(sh|ps1); demo and eval scripts reuse it."""
    for name in ("serve_local.sh", "serve_local.ps1"):
        text = (ROOT / "scripts" / name).read_text()
        assert text.count("127.0.0.1") >= 2 and "--api-key" in text and "0.0.0.0" not in text, name
    for name in ("demo.sh", "demo.ps1", "eval_local.sh", "eval_local.ps1", "setup_local.sh", "setup_local.ps1"):
        text = (ROOT / "scripts" / name).read_text()
        assert "0.0.0.0" not in text, name
        assert "llama-server -m" not in text and "Start-Process $llama" not in text, f"{name} starts llama-server itself"
    sh, ev = (ROOT / "scripts/demo.sh").read_text(), (ROOT / "scripts/eval_local.sh").read_text()
    for text in (sh, ev):
        assert re.search(r"redis-server --bind 127\.0\.0\.1 .*--requirepass", text)
        assert "uvicorn bankql.api:app --host 127.0.0.1" in text


def test_compose_publishes_only_on_localhost():
    dc = (ROOT / "docker-compose.yml").read_text()
    for port in re.findall(r'ports: \[(.*?)\]', dc):
        assert port.strip('"').startswith(("127.0.0.1:", "${BANKQL_BIND:-127.0.0.1}:")), port
    assert "--requirepass" in dc and "BANKQL_LLAMA_API_KEY" in dc
