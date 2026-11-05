# One-time local setup (Windows, GPU workstation): Python env, secrets in .env, PostgreSQL + Redis in Docker
# (127.0.0.1 only, both databases bankql_fixture + bankql_bird with role bankql_ro), synthetic fixture (tests),
# BIRD mini-dev "financial" (original SQLite untouched; DuckDB + Postgres copies), Arctic GGUF download, your Gemma
# GGUF, DuckDB extensions for the pinned DuckDB, and a check of every source.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup_local.ps1 -Gemma C:\models\gemma-3-12b-it-Q4_K_M.gguf
# Needs: uv, Docker Desktop, internet.
param([Parameter(Mandatory = $true)][string]$Gemma, [switch]$SkipBird, [switch]$SkipModels)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$birdUrl = "https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip"
function Step($t) { Write-Host "`n== $t" }
function Rand { -join ((1..24) | ForEach-Object { [char](Get-Random -InputObject (48..57 + 65..90 + 97..122)) }) }
function SetEnv($k, $v) {
  $lines = @(); if (Test-Path .env) { $lines = Get-Content .env | Where-Object { $_ -notmatch "^$k=" } }
  ($lines + "$k=$v") | Set-Content .env; Set-Item "env:$k" $v
}

Step "Python environment (uv, pinned lock)"
uv sync --frozen
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
$cur = Get-Content .env -Raw
foreach ($k in "BANKQL_LLAMA_API_KEY", "BANKQL_REDIS_PASSWORD", "BANKQL_PG_ADMIN_PASSWORD", "BANKQL_PG_RO_PASSWORD") {
  if ($cur -notmatch "(?m)^$k=") { SetEnv $k (Rand) }
}
if ($cur -match "(?m)^BANKQL_API_KEYS=change-me") { SetEnv "BANKQL_API_KEYS" "local-$(Rand)" }
if ($cur -notmatch "(?m)^BANKQL_ADMIN_KEYS=.+") { SetEnv "BANKQL_ADMIN_KEYS" "admin-$(Rand)" }  # /admin: plug in data
Get-Content .env | Where-Object { $_ -match "^[A-Z_]+=" } | ForEach-Object { $k, $v = $_ -split "=", 2; Set-Item "env:$k" $v }
$admin = $env:BANKQL_PG_ADMIN_PASSWORD; $ro = $env:BANKQL_PG_RO_PASSWORD
SetEnv "BANKQL_PG_ADMIN_URL" "postgresql+psycopg://bankql_admin:$admin@127.0.0.1:5432/bankql_fixture"
SetEnv "BANKQL_PG_URL" "postgresql+psycopg://bankql_ro:$ro@127.0.0.1:5432/bankql_fixture"
SetEnv "BANKQL_BIRD_PG_ADMIN_URL" "postgresql+psycopg://bankql_admin:$admin@127.0.0.1:5432/bankql_bird"
SetEnv "BANKQL_BIRD_PG_URL" "postgresql+psycopg://bankql_ro:$ro@127.0.0.1:5432/bankql_bird"
SetEnv "BANKQL_REDIS_URL" "redis://:$($env:BANKQL_REDIS_PASSWORD)@127.0.0.1:6379/0"

Step "PostgreSQL + Redis (docker compose; scripts\pg_init.sh creates roles and both databases)"
docker compose up -d postgres redis
foreach ($i in 1..60) { docker compose exec -T postgres pg_isready -U postgres 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { break }; Start-Sleep 2 }

Step "Synthetic fixture (used by the test suite)"
.venv\Scripts\python scripts\make_fixture.py

if (-not $SkipBird) {
  Step "BIRD mini-dev -> data\bird (original SQLite untouched) -> DuckDB + Postgres copies"
  New-Item -ItemType Directory -Force data\bird | Out-Null
  if (-not (Test-Path data\bird\minidev.zip)) { Invoke-WebRequest $birdUrl -OutFile data\bird\minidev.zip -UseBasicParsing }
  Expand-Archive data\bird\minidev.zip -DestinationPath data\bird -Force
  .venv\Scripts\python scripts\load_bird.py --bird-dir data\bird
}

if (-not $SkipModels) {
  Step "Models: Arctic-Text2SQL-R1-7B i1-IQ4_XS (download) + your Gemma GGUF"
  $m = (.venv\Scripts\python -c "import yaml;m=yaml.safe_load(open('config/models.yaml'))['sql'];print(m['hf_repo'], m['hf_file'])") -split " "
  New-Item -ItemType Directory -Force models | Out-Null
  if (-not (Test-Path "models\$($m[1])")) { uvx --from "huggingface_hub[cli]" hf download $m[0] $m[1] --local-dir models }
  SetEnv "BANKQL_SQL_GGUF" (Resolve-Path "models\$($m[1])").Path
  if (-not (Test-Path $Gemma)) { throw "Gemma GGUF not found: $Gemma" }
  SetEnv "BANKQL_ROUTER_GGUF" (Resolve-Path $Gemma).Path
}

Step "DuckDB extensions for the pinned DuckDB (sqlite_scanner, postgres_scanner)"
.venv\Scripts\python -c "import duckdb; c = duckdb.connect(); [ (c.execute('INSTALL ' + e), c.execute('LOAD ' + e)) for e in ('sqlite_scanner', 'postgres_scanner') ]; print('duckdb', duckdb.__version__, 'extensions ok')"

Step "Check every source (read-only is inspected, never tested by writing)"
$sources = @("financial", "bank_demo", "bank_pg"); if (-not $SkipBird) { $sources += @("bird_financial", "bird_demo", "bird_pg") }
foreach ($s in $sources) { Write-Host "-- $s"; .venv\Scripts\bankql check-source $s; if ($LASTEXITCODE -ne 0) { Write-Host "!! $s has FAIL lines above" } }
Write-Host "`nSetup done. Next: scripts\serve_local.ps1 -Detach   then   scripts\eval_local.ps1"
Write-Host "Your own data: http://127.0.0.1:8000/admin (admin key: BANKQL_ADMIN_KEYS in .env), or: .venv\Scripts\bankql import <name> file.csv"
