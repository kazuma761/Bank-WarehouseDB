# Running BankQL on your PC (RTX 4080 Super)

This guide covers what to install, how to run everything, what a good result looks like, and how to fix common
problems. At the end there is a prompt you can paste into a coding agent (Claude Code or similar) running on the same
PC, so it can do the work for you.

## 1. What runs where

All services bind to `127.0.0.1` only; nothing is reachable from your network.

| Service | Port | Started by |
|---|---|---|
| Gemma 3 12B (router, rewrite, summaries), llama-server | 8080 | `scripts/serve_local.*` |
| Arctic-Text2SQL-R1-7B (SQL), llama-server, 3 slots | 8081 | `scripts/serve_local.*` |
| BankQL REST API + `/demo` page | 8000 | `scripts/demo.*` or uvicorn |
| PostgreSQL 16 (`bankql_fixture`, `bankql_bird`) | 5432 | system service (Linux) / Docker (Windows) |
| Redis (schema cache, password-protected) | 6379 | `scripts/eval_local.*` / `scripts/demo.*` |

The data is the public BIRD mini-dev "financial" database plus a synthetic fixture. Do not load real customer data.

## 2. Hardware

- **GPU:** NVIDIA, 16 GB VRAM (RTX 4080 Super). Both models stay loaded together: about 7.3 GB + 4.2 GB plus KV cache.
- **RAM:** 32 GB recommended.
- **Disk:** about 20 GB free (models ~12 GB, BIRD ~1.5 GB, Python env, databases).

## 3. Choose a path

- **A. Linux, or Windows with WSL2 (Ubuntu 24.04): recommended.** The `.sh` scripts and the test suite were
  verified on Linux.
- **B. Native Windows (PowerShell).** The `.ps1` scripts exist but have **not** been run yet. Expect to fix small
  issues.

## 4. Packages to install

### Path A: Linux / WSL2 Ubuntu 24.04

| Package | Why | Install |
|---|---|---|
| NVIDIA driver | GPU | Linux: distro driver (`sudo ubuntu-drivers install`). **WSL2: install the driver on Windows only, never inside WSL.** |
| CUDA toolkit 12.x | build llama.cpp with CUDA | `sudo apt install nvidia-cuda-toolkit`, or NVIDIA's CUDA repo (in WSL use the "WSL-Ubuntu" package) |
| git, build tools, cmake | clone + build llama.cpp | `sudo apt install git build-essential cmake` |
| curl, unzip | BIRD download | `sudo apt install curl unzip` |
| PostgreSQL 16 | Postgres sources | `sudo apt install postgresql-16` (Ubuntu 24.04 ships 16; on 22.04 add the PGDG apt repo first) |
| Redis | schema cache | `sudo apt install redis-server` |
| python3 | small helpers in the scripts | normally preinstalled |
| uv | Python 3.12 env from `uv.lock` (it downloads Python 3.12 itself) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| llama.cpp (CUDA build) | `llama-server` | build from source, step 5.2 below |

Python packages are **not** installed by hand: `uv sync --frozen` installs the exact versions from `uv.lock`,
including DuckDB 2.0.0, which is pinned.

### Path B: native Windows

| Package | Install |
|---|---|
| NVIDIA driver (recent, CUDA 12 capable) | nvidia.com or GeForce Experience |
| Git for Windows | `winget install Git.Git` |
| uv | `winget install astral-sh.uv` |
| Docker Desktop (runs Postgres + Redis) | `winget install Docker.DockerDesktop` |
| llama.cpp CUDA build | From https://github.com/ggml-org/llama.cpp/releases download the `win-cuda` x64 zip **and** the matching `cudart` zip, unzip both into one folder, e.g. `C:\llama`. Set `LLAMA_SERVER=C:\llama\llama-server.exe` or add the folder to PATH. |

## 5. Step by step (Path A)

### 5.1 Get the code

```bash
git clone --recurse-submodules https://github.com/kazuma761/Bank-WarehouseDB.git bankql
```
```bash
cd bankql
```

The submodules (`third_party/mercer`, `third_party/bird_mini_dev`) are needed; the BIRD scoring test uses
`bird_mini_dev`. If you cloned without them, run `git submodule update --init`.

### 5.2 Build llama.cpp with CUDA

```bash
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp
```
```bash
cmake -S ~/llama.cpp -B ~/llama.cpp/build -DGGML_CUDA=ON && cmake --build ~/llama.cpp/build --config Release -j
```
```bash
echo 'export LLAMA_SERVER=$HOME/llama.cpp/build/bin/llama-server' >> ~/.bashrc && source ~/.bashrc
```

Check: `$LLAMA_SERVER --version` prints a version, and `nvidia-smi` shows the 4080 Super.

### 5.3 Download Gemma

Arctic is downloaded by the setup script; Gemma you download once yourself:

```bash
uvx --from "huggingface_hub[cli]" hf download unsloth/gemma-3-12b-it-GGUF gemma-3-12b-it-Q4_K_M.gguf --local-dir models
```

If Hugging Face asks you to log in or accept a licence, run `uvx --from "huggingface_hub[cli]" hf auth login` first.

### 5.4 One-time setup

```bash
scripts/setup_local.sh --gemma models/gemma-3-12b-it-Q4_K_M.gguf
```

It asks for your sudo password once, to create the Postgres roles. It does the following, in order:

1. Creates the Python env with `uv sync --frozen`.
2. Writes `.env` (mode 600) with random secrets.
3. Creates the Postgres roles `bankql_admin` and `bankql_ro` (read-only) and the databases `bankql_fixture` and
   `bankql_bird`.
4. Builds the synthetic fixture.
5. Downloads BIRD mini-dev into `data/bird` (the original SQLite is never modified) and copies it to DuckDB and
   Postgres.
6. Downloads Arctic.
7. Installs the DuckDB extensions.
8. Runs `bankql check-source` on all six sources.

**Good result:** every source prints only OK/WARN lines, with no `!! … has FAIL lines`.

### 5.5 Run the tests (no models needed)

```bash
.venv/bin/python -m pytest -m "not real_llm" -q
```

Expect about 574 passed and a handful skipped. Federation tests skip if the DuckDB extensions are missing.

### 5.6 Start the models

```bash
scripts/serve_local.sh --detach
```

**Good result:** `router ready on 127.0.0.1:8080` and `sql ready on 127.0.0.1:8081`. Logs are in
`data/logs/router.log` and `data/logs/sql.log`. Stop them with `scripts/serve_local.sh --stop`.

### 5.7 Run the evaluation

Expect a few hours; the BIRD analytics step is the longest.

```bash
scripts/eval_local.sh
```

You can run only some steps: `scripts/eval_local.sh lookup analytics chat latency report`. Every step is resumable:
if it stops, run the same command again and finished items are skipped.

**Output:** raw results go to `eval/results/` and `REPORT.md` is regenerated. Then do two things by hand:

1. Fill in the 10-row spot-check table in `REPORT.md`.
2. Set `router.confidence_threshold` in `config/models.yaml` from the calibration table.

### 5.8 Try the demo

```bash
scripts/demo.sh
```

Open `http://127.0.0.1:8000/demo`. The API key is the first value of `BANKQL_API_KEYS` in `.env`. Ctrl-C stops what
the script started.

## 6. Step by step (Path B, PowerShell)

Run from the repo folder, with Docker Desktop running:

```powershell
git clone --recurse-submodules https://github.com/kazuma761/Bank-WarehouseDB.git bankql; cd bankql
```
```powershell
uvx --from "huggingface_hub[cli]" hf download unsloth/gemma-3-12b-it-GGUF gemma-3-12b-it-Q4_K_M.gguf --local-dir models
```
```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_local.ps1 -Gemma models\gemma-3-12b-it-Q4_K_M.gguf
```
```powershell
.venv\Scripts\python -m pytest -m "not real_llm" -q
```
```powershell
powershell -ExecutionPolicy Bypass -File scripts\serve_local.ps1 -Detach
```
```powershell
powershell -ExecutionPolicy Bypass -File scripts\eval_local.ps1
```
```powershell
powershell -ExecutionPolicy Bypass -File scripts\demo.ps1
```

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `llama-server not found` | Set `LLAMA_SERVER` to the full path of the binary. |
| `-fa on` rejected / model won't start | Older llama.cpp: set `BANKQL_FA_FLAG=-fa`. |
| CUDA out of memory | Close other GPU apps; check `nvidia-smi`. Last resort: `BANKQL_NGL=40` (slower; offloads fewer layers). |
| BIRD download slow or fails | Download `minidev.zip` from the BIRD mini-dev page yourself and save it as `data/bird/minidev.zip`; setup skips the download when the file exists. |
| `check-source` FAIL on `bank_pg`/`bird_pg` | Postgres not running (`sudo service postgresql start`), or stale passwords: re-run `sudo scripts/setup_postgres.sh`. |
| DuckDB `INSTALL` fails | The host `extensions.duckdb.org` must be reachable. Only federation needs it. |
| Redis auth errors | The password lives in `.env` (`BANKQL_REDIS_PASSWORD`); stop any other Redis on port 6379. |
| Eval stopped halfway | Re-run the same command; finished items are skipped. |

## 8. Rules

- Keep everything on `127.0.0.1`.
- Never commit `.env`, `models/`, `data/` or the raw `eval/results/*.jsonl` (all git-ignored).
- No hosted LLM APIs; models run only through llama-server.
- Public BIRD data and synthetic data only.

---

## 9. Prompt to hand to a coding agent on your PC

Open the agent in the cloned repo folder and paste this:

```text
You are setting up and running BankQL on this PC (NVIDIA RTX 4080 Super, 16 GB). Follow docs/RUN_ON_PC.md exactly; read it
first, then README.md "Run locally on GPU" and the scripts it calls (scripts/setup_local.*, serve_local.*, eval_local.*,
demo.*) before running them.

Goal, in order:
1. Detect the OS and pick Path A (Linux/WSL2, preferred) or Path B (native Windows). Tell me which and why.
2. Check prerequisites from section 4 (nvidia-smi, CUDA toolkit or llama.cpp release, git, cmake, uv, Postgres 16 + Redis or
   Docker Desktop, curl/unzip). List what is missing and the exact install commands, then ask me before installing anything
   system-wide or anything that needs sudo/admin.
3. Build or locate llama-server with CUDA and confirm `llama-server --version`.
4. Download Gemma (section 5.3) if models/gemma-3-12b-it-Q4_K_M.gguf is missing.
5. Run the one-time setup (5.4). Show me every `check-source` FAIL line, if any, and fix the cause before continuing.
6. Run the tests (5.5) and report passed/failed/skipped counts. If something fails, show the failing test output and diagnose
   before changing anything.
7. Start the models (5.6), confirm both are ready, and record `nvidia-smi` memory use.
8. Run scripts/eval_local.* (5.7). It is resumable; if it stops, re-run the same command. Report progress per step.
9. Summarize REPORT.md for me: accuracy tables with sample sizes, latency, the router calibration table. Do NOT fill in the
   spot-check table or change config/models.yaml thresholds yourself; list what I need to decide.

Rules:
- Do not edit source code, configs, prompts, semantic YAML or eval datasets to make numbers better. If you think a code change
  is needed, stop and explain it to me first.
- Never put evaluation questions or gold SQL into prompts, the glossary, verified queries or the semantic model.
- Keep all services on 127.0.0.1. Never print or commit .env or secrets. No hosted LLM APIs.
- Do not modify data/bird's original financial.sqlite.
- Don't run destructive commands (dropping databases, deleting data/ or models/, force-pushing) without asking.
- If something blocks you (download fails, GPU out of memory, missing permission), stop and tell me what you tried.
- When done, commit only REPORT.md and eval/results/*.txt (raw *.jsonl stay git-ignored; no secrets, models or data) on a
  new branch `local-eval-<date>` and push it; then show me the branch name. Ask before opening a pull request.
```
