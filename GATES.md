# Gates: BankQL steps 1–9 (no-download build)

OWNS: bankql/**, config/**, scripts/**, tests/**, eval/**, examples/**, docs/**, SPEC.md, PROGRESS.md, GATES.md, README.md, Dockerfile, docker-compose.yml, pyproject.toml, uv.lock, requirements.lock, .gitignore, .gitmodules, third_party/**

Scope: Steps 1–9 from the owner's 2026-11-04 instruction — skeleton, pinned third-party reads, synthetic fixture in three DBs, Phase 2 core, semantic YAML + Mercer mappings adapter, AnalyticsEngine + Mercer adapter on FakeLLM, four integration surfaces, resumable eval harness, llama-cpp-python build — with downloads, real-model evals and GPU testing deferred to PROGRESS.md "Later".

- [x] G1: Repo layout from SPEC.md section 9 exists and is a git repo with commits
  CHECK: .venv/bin/python scripts/gate.py layout
  EXPECT: GATE layout OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=b51a08fcea1f67fb5cf3f8a45ba48169fe431d3acf929ae728d85287e0439f86; exit=0; EXPECT=matched; output-sha256=292257d5bf6ca2b1d71418232e2f25644c366290956386edca1c01aebd913299; output-bytes=15; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G2: Mercer and BIRD mini_dev are pinned submodules whose HEADs match the hashes recorded in docs/THIRD_PARTY.md, and installed sqlas matches the lock
  CHECK: .venv/bin/python scripts/gate.py pins
  EXPECT: GATE pins OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=8c4f6269ef4ff2b1fd91fab86c150d2d8fe9c9e95bb3d9267ad8c3931cf494d1; exit=0; EXPECT=matched; output-sha256=53a8a61f5a4cf25b967f18f3c70e009652600897fe9fa376cb7094c2cc0dd29c; output-bytes=13; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G3: Synthetic fixture (8 BIRD financial tables incl. A2–A16, Czech codes, "order", plus client_profile) loads into SQLite, DuckDB and Postgres with identical per-table row counts
  CHECK: .venv/bin/python scripts/gate.py fixture
  EXPECT: GATE fixture OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=8b553462e85b43cffbadcd30cc38b6dc499f30c4c379b2d9656a09b97d47551c; exit=0; EXPECT=matched; output-sha256=5e6f04c927d20ebf5fb8a3feb667acd334ef3232c0af1ceff4bdd5814cd9754a; output-bytes=190; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G4: Validator rejects DDL/DML (top level, in CTEs, in subqueries), multi-statement input and unfiltered large-table scans, while a known-good SELECT on the same table passes (positive control)
  CHECK: .venv/bin/python scripts/gate.py validator
  EXPECT: GATE validator OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=4e36b85548d646a0fceb7880e22b8321e20f5db36dc7c7164d52ce3d1b6805f1; exit=0; EXPECT=matched; output-sha256=966337e2cd166a1e63f96014ba4e57ccad948d4359fb325d7c009d258f7ca0ea; output-bytes=200; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G5: Phase 2 core test suite (connectors, guardrails incl. PII lineage, resolver, lookup tools, exporter, audit) passes on SQLite, DuckDB and Postgres with no skipped tests
  CHECK: .venv/bin/python scripts/gate.py core
  EXPECT: GATE core OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=be76f62d6ee331e63c81dbd288c973e93bebe55dd9af553dc29784a69930ca5d; exit=0; EXPECT=matched; output-sha256=053e38cef0e3aaeab26056dd70ed72ad5977a91c84047aa8e851beae3e0a039b; output-bytes=275; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [ ] G6: DuckDB federation joins the SQLite fixture to the Postgres copy in one query
  CHECK: .venv/bin/python scripts/gate.py federation
  EXPECT: GATE federation OK
  EVIDENCE: pending

- [x] G7: Semantic YAML loads, and the generated Mercer mappings.yaml is accepted by the ported Mercer SemanticMapper with every table and column described
  CHECK: .venv/bin/python scripts/gate.py semantic
  EXPECT: GATE semantic OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=613209f991b683ba75d3f802fc4c52a18fd3d6453227fc5d2f2795abff26484b; exit=0; EXPECT=matched; output-sha256=e787dbfdec3b135d8056aea54d26a67e6c7a674c934b4cbdfb1a65e5ed011960; output-bytes=117; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G8: AnalyticsEngine fast mode and full mode answer fixture questions end to end on FakeLLM, and importing the analytics stack never imports torch or triton
  CHECK: .venv/bin/python scripts/gate.py analytics
  EXPECT: GATE analytics OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=7cda8912fedea77e0b5ab3aa0d692c3730b01b4e9ebe0c7224b9fd0d6c27cbfc; exit=0; EXPECT=matched; output-sha256=137ca874245b98b89fd804be31fcfc20f34ca80d5ac36e1317f98da274ac57e8; output-bytes=119; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G9: Python library, REST API (incl. auth, SSE, CSV export), MCP server and OpenAI-compatible endpoint pass end-to-end tests on FakeLLM
  CHECK: .venv/bin/python scripts/gate.py surfaces
  EXPECT: GATE surfaces OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=0ee37d72cd81f754eea1629f7598bda7d8160fcd237e86eb8db1c1b7b8b65cbd; exit=0; EXPECT=matched; output-sha256=411d14b42faa664344d1f9b9b1657b73ce3110b3a0b650d8e63b63472b9ff784; output-bytes=199; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G10: Eval harness resumes from a partial JSONL without redoing items, and our BIRD EX equals the official mini_dev calculate_ex on every test case
  CHECK: .venv/bin/python scripts/gate.py eval
  EXPECT: GATE eval OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=8365047d476d6d2016554c55ea7207ec8dee47c4fe481c9361f3b5eb382b2950; exit=0; EXPECT=matched; output-sha256=e06f9e2f59270494923c0736080796a6066ed3832890c435d49c169bd18c6a3e; output-bytes=114; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G11: llama-cpp-python[server] built from PyPI imports its server module in .venv-llama
  CHECK: .venv/bin/python scripts/gate.py llama
  EXPECT: GATE llama OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=4515f6fef4186bafd0c1ed11bcf42683e4328d98dd3a2f163c9959720ec4996c; exit=0; EXPECT=matched; output-sha256=b75f3a012a48ecb6b338b64d2a7728a8ebd48ad5cc1e1bddde42f948bdd8c807; output-bytes=23; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [ ] G12: Full pytest suite is green with skips limited to the real_llm marker
  CHECK: .venv/bin/python scripts/gate.py all
  EXPECT: GATE all OK
  EVIDENCE: pending

- [x] G13: PROGRESS.md has a "Later" section naming exactly what unblocks BIRD download, model downloads, Phase 3–4 real-model evals, Phase 6 GPU testing, and per-user identity → DB roles
  EVIDENCE: manual review 2026-11-04 of PROGRESS.md "Later": #1 BIRD (allow bird-bench.oss-cn-beijing.aliyuncs.com + steps), #2 models (allow huggingface.co, cdn-lfs.huggingface.co, cas-bridge.xethub.hf.co; owner Gemma repo/file; HF_TOKEN if gated), #4 Phase 3 and #5 Phase 4 (needs #1-#3, commands given), #7 Phase 6 (scripts to write; needs RTX 4080 Super), #8 per-user identity -> DB roles. Also #3 structured-output check, #6 real-model smoke tests, #9 federation + Docker blockers.

ABANDON: G6 Blocked by the session network policy: DuckDB's sqlite_scanner/postgres_scanner extensions download from extensions.duckdb.org (denied). Handoff: owner allows that host, approves the third-party PyPI extension wheels, or approves a copy-into-DuckDB fallback (PROGRESS.md "Blockers" #1).
ABANDON: G12 The only non-real_llm skip is the G6 federation test (same blocker); everything else passes. Handoff: resolves with G6.
