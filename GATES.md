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
  EVIDENCE: automatic-evidence=v1; definition-sha256=4e36b85548d646a0fceb7880e22b8321e20f5db36dc7c7164d52ce3d1b6805f1; exit=0; EXPECT=matched; output-sha256=0a6d97da2589d517b30c0c25f770b577c54e18f9581234f87fe00e7fb24fb559; output-bytes=200; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G5: Phase 2 core test suite (connectors, guardrails incl. PII lineage, resolver, lookup tools, exporter, audit) passes on SQLite, DuckDB and Postgres with no skipped tests
  CHECK: .venv/bin/python scripts/gate.py core
  EXPECT: GATE core OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=be76f62d6ee331e63c81dbd288c973e93bebe55dd9af553dc29784a69930ca5d; exit=0; EXPECT=matched; output-sha256=d13483c1986cedbfae14f91ed7ccef4aa3652465e2f22034de6aacff3f84ed82; output-bytes=275; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [ ] G6: DuckDB federation joins the SQLite fixture to the Postgres copy in one query
  CHECK: .venv/bin/python scripts/gate.py federation
  EXPECT: GATE federation OK
  EVIDENCE: pending

- [x] G7: Semantic YAML loads, and the generated Mercer mappings.yaml is accepted by the ported Mercer SemanticMapper with every table and column described
  CHECK: .venv/bin/python scripts/gate.py semantic
  EXPECT: GATE semantic OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=613209f991b683ba75d3f802fc4c52a18fd3d6453227fc5d2f2795abff26484b; exit=0; EXPECT=matched; output-sha256=11e9653946fc68fb12d66e32cb628b7e586a81d35e4be7ae180bbd483a6ac552; output-bytes=117; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G8: AnalyticsEngine fast mode and full mode answer fixture questions end to end on FakeLLM, and importing the analytics stack never imports torch or triton
  CHECK: .venv/bin/python scripts/gate.py analytics
  EXPECT: GATE analytics OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=7cda8912fedea77e0b5ab3aa0d692c3730b01b4e9ebe0c7224b9fd0d6c27cbfc; exit=0; EXPECT=matched; output-sha256=4f46a0e2786d5bb764dc774dc8adc98906e017f4ed4395da53ca6d8ccd154fef; output-bytes=119; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G9: Python library, REST API (incl. auth, SSE, CSV export), MCP server and OpenAI-compatible endpoint pass end-to-end tests on FakeLLM
  CHECK: .venv/bin/python scripts/gate.py surfaces
  EXPECT: GATE surfaces OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=0ee37d72cd81f754eea1629f7598bda7d8160fcd237e86eb8db1c1b7b8b65cbd; exit=0; EXPECT=matched; output-sha256=eda6f8a987b4758f77215c50c15663f4a605c046f0785214d47b21ada9de305b; output-bytes=199; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G10: Eval harness resumes from a partial JSONL without redoing items, and our BIRD EX equals the official mini_dev calculate_ex on every test case
  CHECK: .venv/bin/python scripts/gate.py eval
  EXPECT: GATE eval OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=8365047d476d6d2016554c55ea7207ec8dee47c4fe481c9361f3b5eb382b2950; exit=0; EXPECT=matched; output-sha256=e2adf08620c10353956e365f50ddf484ce1f87f011b011e114d05779969ec902; output-bytes=114; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

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

## Round 2 (2026-11-04 instruction: E, F, B, D, C + Docker/SQLAS/DuckDB/Gemma decisions)

- [x] G14: The local bankql branch is pushed: origin/bankql on kazuma761/Bank-WarehouseDB equals local HEAD
  CHECK: .venv/bin/python scripts/gate.py pushed
  EXPECT: GATE pushed OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=3d07a99ead21755779f6c4238ca8c8061d10ac5245f535e6ac01cf972e9cfc74; exit=0; EXPECT=matched; output-sha256=a78a7428b185ca83672c71f52d0da0aace87df3a5775db1eee72f9ecd15b3efd; output-bytes=15; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G15: /demo works in a real browser through /v1/ask with session_id: three questions incl. a follow-up render cards with route, SQL, table and per-stage latency, no console errors
  CHECK: .venv/bin/python scripts/gate.py demo
  EXPECT: GATE demo OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=446dd33e8cbe806d32ff221547a4c8eae954c459500c47f85fdd96dfb7598c39; exit=0; EXPECT=matched; output-sha256=fe757ba889369f48882a92b5b89ca59bb2e40da34cd741b33b0f1501ebd4079b; output-bytes=67; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G16: scripts/demo.sh parses, and demo.sh / demo.ps1 / compose bind llama-server, Redis and the API to 127.0.0.1 with keys/passwords
  CHECK: .venv/bin/python scripts/gate.py demo_scripts
  EXPECT: GATE demo_scripts OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=aebde252b319db2d5d8dfd2d4a7c6cdd959dafef4c98dd66ba2cb2145c089aa3; exit=0; EXPECT=matched; output-sha256=0b788e3c78803547b3b97b1895460c2b4627f70c256dfbcd24d110a93b3c923d; output-bytes=121; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G17: docs/CONNECT.md covers app→BankQL (schema incl. user_id, errors, timeouts, exports, curl/Python/JS/Java), six DB templates with read-only-user SQL, large tables, and every environment variable the code reads
  CHECK: .venv/bin/python scripts/gate.py connect_doc
  EXPECT: GATE connect_doc OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=cc8098b3dd327c2df7c7572112e7622d5da6511f7dc3b293a68d45825cec7230; exit=0; EXPECT=matched; output-sha256=94e32a34b9588463dc3fc3812aaf33a3d90f17e0f2dbd21a751d22e1d7a7cf71; output-bytes=20; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G18: bankql check-source passes on SQLite/DuckDB/Postgres, fails on the writable admin role without attempting a write, and init-source writes a loadable skeleton
  CHECK: .venv/bin/python scripts/gate.py cli
  EXPECT: GATE cli OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=96d027c7beeb63e3854cd1b712ccba20ef247d473b0f7b922a1baaaa255d15d9; exit=0; EXPECT=matched; output-sha256=5db0171625d323e5ea1265d198d2895c47d3ee03261939778829104403678074; output-bytes=113; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G19: eval/chat_smoke.py runs at least 30 conversations through /v1/ask (FakeLLM) and every turn's outcome matches its fixture expectation, with route, SQL, rows and refusals logged per turn
  CHECK: .venv/bin/python scripts/gate.py chat_smoke
  EXPECT: GATE chat_smoke OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=90669c96c8b39526f9556779aee4fc4ae1851371065233ccca2315b10c531456; exit=0; EXPECT=matched; output-sha256=f23f9a887490d52eaa28c5c1e14b8aa30cff815b6612d45aff053b1e5a918942; output-bytes=63; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G20: The adversarial suite tests/security passes with no skips and docs/SECURITY.md reports its results and residual risks
  CHECK: .venv/bin/python scripts/gate.py security
  EXPECT: GATE security OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=1a06feb4e6eb3c4d763f86257f09461931db92a7226ec72220636d2ebf3bbbac; exit=0; EXPECT=matched; output-sha256=9e04a94f7fa9525a819130dfe6f45a40a786c0e92cbd5b055c76c294aea20bfe; output-bytes=36; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G21: Latency report measured before vs after (FakeLLM): per-stage p50/p95, LLM calls and prompt size per question, cProfile shares of LLM / our Python / DB, and the optimisations' effect
  CHECK: .venv/bin/python scripts/gate.py latency
  EXPECT: GATE latency OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=2d031f5053b96d4240b332c82354cfd1497b6ab8e4c8184a6390f6f278880a46; exit=0; EXPECT=matched; output-sha256=893d98ab095c1db74886c96616ec01741e410b6a9062f36b607d7cb3295adeb7; output-bytes=71; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [ ] G22: Docker image builds from the ECR base image (REGISTRY build ARG)
  CHECK: .venv/bin/python scripts/gate.py docker
  EXPECT: GATE docker OK
  EVIDENCE: pending

- [x] G23: DuckDB is pinned to an exact version in pyproject.toml and PROGRESS.md "Later" notes shipping extension files for offline installs; Gemma GGUF is configured
  CHECK: .venv/bin/python scripts/gate.py pins2
  EXPECT: GATE pins2 OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=3acab02fe8546710109cfa7eeef117c2de14e8c14add1d86a8f4fbc619da513e; exit=0; EXPECT=matched; output-sha256=d7012fa759badc269aeec680f1f6298df34a4ff0ad4a6884394eb23dfe907961; output-bytes=14; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [x] G24: Full suite green; the only skip is the federation test reporting the DuckDB-extension blocker
  CHECK: .venv/bin/python scripts/gate.py all2
  EXPECT: GATE all2 OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=1630ac9a24219901f5881d14f5437f9cb80beb632a011aa22cbf3fe037521a6b; exit=0; EXPECT=matched; output-sha256=9548813371e5c64ed4cfb4a5abf0b0530ccdc5acc6ae0f9f816daae3cc4f55ba; output-bytes=545; shell=/bin/sh; cwd=/home/user/bankql-repo; path=7f4fb4b02918/15 entries

- [ ] G25: A (real BIRD data): bird_financial loaded in SQLite/DuckDB/Postgres and the suite re-run against it
  CHECK: .venv/bin/python scripts/gate.py bird
  EXPECT: GATE bird OK
  EVIDENCE: pending

ABANDON: G22 ECR registry reachable but its image layers come from d2glxqk2uabbnd.cloudfront.net, which the proxy forbids. Handoff: owner allows that host, then re-run `scripts/gate.py docker`.
ABANDON: G25 bird-bench.oss-cn-beijing.aliyuncs.com is still denied by the network policy (re-checked 2026-11-04). Handoff: allowed-domain change takes effect in a new session; then Phase 1 real data.
