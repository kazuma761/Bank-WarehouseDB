# Gates: BankQL steps 1–9 (no-download build)

OWNS: bankql/**, config/**, scripts/**, tests/**, eval/**, examples/**, docs/**, SPEC.md, PROGRESS.md, GATES.md, README.md, Dockerfile, docker-compose.yml, pyproject.toml, uv.lock, requirements.lock, .gitignore, .gitmodules, third_party/**

Scope: Steps 1–9 from the owner's 2026-11-04 instruction — skeleton, pinned third-party reads, synthetic fixture in three DBs, Phase 2 core, semantic YAML + Mercer mappings adapter, AnalyticsEngine + Mercer adapter on FakeLLM, four integration surfaces, resumable eval harness, llama-cpp-python build — with downloads, real-model evals and GPU testing deferred to PROGRESS.md "Later".

- [ ] G1: Repo layout from SPEC.md section 9 exists and is a git repo with commits
  CHECK: .venv/bin/python scripts/gate.py layout
  EXPECT: GATE layout OK
  EVIDENCE: pending

- [ ] G2: Mercer and BIRD mini_dev are pinned submodules whose HEADs match the hashes recorded in docs/THIRD_PARTY.md, and installed sqlas matches the lock
  CHECK: .venv/bin/python scripts/gate.py pins
  EXPECT: GATE pins OK
  EVIDENCE: pending

- [ ] G3: Synthetic fixture (8 BIRD financial tables incl. A2–A16, Czech codes, "order", plus client_profile) loads into SQLite, DuckDB and Postgres with identical per-table row counts
  CHECK: .venv/bin/python scripts/gate.py fixture
  EXPECT: GATE fixture OK
  EVIDENCE: pending

- [ ] G4: Validator rejects DDL/DML (top level, in CTEs, in subqueries), multi-statement input and unfiltered large-table scans, while a known-good SELECT on the same table passes (positive control)
  CHECK: .venv/bin/python scripts/gate.py validator
  EXPECT: GATE validator OK
  EVIDENCE: pending

- [ ] G5: Phase 2 core test suite (connectors, guardrails incl. PII lineage, resolver, lookup tools, exporter, audit) passes on SQLite, DuckDB and Postgres with no skipped tests
  CHECK: .venv/bin/python scripts/gate.py core
  EXPECT: GATE core OK
  EVIDENCE: pending

- [ ] G6: DuckDB federation joins the SQLite fixture to the Postgres copy in one query
  CHECK: .venv/bin/python scripts/gate.py federation
  EXPECT: GATE federation OK
  EVIDENCE: pending

- [ ] G7: Semantic YAML loads, and the generated Mercer mappings.yaml is accepted by the ported Mercer SemanticMapper with every table and column described
  CHECK: .venv/bin/python scripts/gate.py semantic
  EXPECT: GATE semantic OK
  EVIDENCE: pending

- [ ] G8: AnalyticsEngine fast mode and full mode answer fixture questions end to end on FakeLLM, and importing the analytics stack never imports torch or triton
  CHECK: .venv/bin/python scripts/gate.py analytics
  EXPECT: GATE analytics OK
  EVIDENCE: pending

- [ ] G9: Python library, REST API (incl. auth, SSE, CSV export), MCP server and OpenAI-compatible endpoint pass end-to-end tests on FakeLLM
  CHECK: .venv/bin/python scripts/gate.py surfaces
  EXPECT: GATE surfaces OK
  EVIDENCE: pending

- [ ] G10: Eval harness resumes from a partial JSONL without redoing items, and our BIRD EX equals the official mini_dev calculate_ex on every test case
  CHECK: .venv/bin/python scripts/gate.py eval
  EXPECT: GATE eval OK
  EVIDENCE: pending

- [ ] G11: llama-cpp-python[server] built from PyPI imports its server module in .venv-llama
  CHECK: .venv/bin/python scripts/gate.py llama
  EXPECT: GATE llama OK
  EVIDENCE: pending

- [ ] G12: Full pytest suite is green with skips limited to the real_llm marker
  CHECK: .venv/bin/python scripts/gate.py all
  EXPECT: GATE all OK
  EVIDENCE: pending

- [ ] G13: PROGRESS.md has a "Later" section naming exactly what unblocks BIRD download, model downloads, Phase 3–4 real-model evals, Phase 6 GPU testing, and per-user identity → DB roles
  EVIDENCE: pending
