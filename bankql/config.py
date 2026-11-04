"""Config loading: config/sources.yaml, config/models.yaml, per-source semantic YAML, .env."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


def load_dotenv(path: str | Path = ".env") -> None:
    """Tiny .env loader (no dependency): KEY=VALUE lines; variables already in the environment win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class ModelEndpoint(BaseModel):
    base_url: str
    base_url_env: str | None = None
    api_key_env: str | None = None
    model: str = "local"
    hf_repo: str | None = None
    hf_file: str | None = None
    timeout_s: float = 120
    structured_output: str = "auto"
    logprobs: bool = True
    confidence_threshold: float = 0.6
    mode: str = "fast"
    redis_url: str = "redis://localhost:6379/0"
    redis_url_env: str | None = None

    def url(self) -> str:
        return os.environ.get(self.base_url_env or "", "") or self.base_url

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env or "") or None

    def redis(self) -> str:
        return os.environ.get(self.redis_url_env or "", "") or self.redis_url


class AgentSettings(BaseModel):
    session_turns: int = 6
    max_validation_attempts: int = 2
    summary_rows: int = 5
    exports_dir: str = "data/exports"
    audit_path: str = "data/audit.duckdb"


class FederationConfig(BaseModel):
    members: list[str]
    row_cap: int = 100_000
    statement_timeout_s: float = 30
    large_scan_threshold_rows: int = 500_000


class AppConfig(BaseModel):
    root: Path
    sources: dict[str, dict]
    federations: dict[str, FederationConfig] = Field(default_factory=dict)
    router: ModelEndpoint
    sql: ModelEndpoint
    agent: AgentSettings = Field(default_factory=AgentSettings)

    def path(self, p: str) -> Path:
        """Resolve a repo-relative path (exports dir, audit db) against the repo root."""
        q = Path(p)
        return q if q.is_absolute() else self.root / q


def load_config(config_dir: str | Path = "config") -> AppConfig:
    cdir = Path(config_dir).resolve()
    root = cdir.parent
    load_dotenv(root / ".env")
    src = yaml.safe_load((cdir / "sources.yaml").read_text())
    models = yaml.safe_load((cdir / "models.yaml").read_text())
    for name, s in src.get("sources", {}).items():
        s["semantic"] = [str((cdir / p).resolve()) for p in s.get("semantic", [])]
    for key, env in (("hf_repo", "BANKQL_ROUTER_HF_REPO"), ("hf_file", "BANKQL_ROUTER_HF_FILE")):
        if os.environ.get(env):
            models["router"][key] = os.environ[env]
    return AppConfig(root=root, sources=src.get("sources", {}), federations=src.get("federations") or {},
                     router=models["router"], sql=models["sql"], agent=models.get("agent", {}))
