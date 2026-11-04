# Windows demo: Postgres + Redis in Docker (127.0.0.1 only), llama-server.exe natively on the GPU, BankQL API.
#   powershell -ExecutionPolicy Bypass -File scripts\demo.ps1
# Needs: Docker Desktop, llama-server.exe on PATH (llama.cpp release, CUDA build), models\ (scripts/fetch_models.sh
# or download the two GGUF files named in config\models.yaml), uv-synced .venv.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force data\logs | Out-Null
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
function Rand { -join ((1..24) | ForEach-Object { [char](Get-Random -InputObject (48..57 + 65..90 + 97..122)) }) }
$envText = Get-Content .env -Raw
foreach ($k in "BANKQL_LLAMA_API_KEY", "BANKQL_REDIS_PASSWORD", "BANKQL_PG_ADMIN_PASSWORD", "BANKQL_PG_RO_PASSWORD") {
  if ($envText -notmatch "(?m)^$k=") { Add-Content .env "$k=$(Rand)" }
}
if ((Get-Content .env -Raw) -match "(?m)^BANKQL_API_KEYS=change-me") {
  (Get-Content .env) -replace "^BANKQL_API_KEYS=.*", "BANKQL_API_KEYS=demo-$(Rand)" | Set-Content .env }
Get-Content .env | Where-Object { $_ -match "^[A-Z_]+=" } | ForEach-Object { $k, $v = $_ -split "=", 2; Set-Item "env:$k" $v }
$env:BANKQL_PG_URL = "postgresql+psycopg://bankql_ro:$($env:BANKQL_PG_RO_PASSWORD)@127.0.0.1:5432/bankql_fixture"
$env:BANKQL_PG_ADMIN_URL = "postgresql+psycopg://bankql_admin:$($env:BANKQL_PG_ADMIN_PASSWORD)@127.0.0.1:5432/bankql_fixture"
$env:BANKQL_REDIS_URL = "redis://:$($env:BANKQL_REDIS_PASSWORD)@127.0.0.1:6379/0"

Write-Host "== Postgres + Redis (docker compose, bound to 127.0.0.1)"
docker compose up -d postgres redis
if (-not (Test-Path data\fixture\financial.sqlite)) { .venv\Scripts\python scripts\make_fixture.py }

$procs = @()
Write-Host "== llama-server (GPU)"
& scripts\serve_local.ps1 -Detach

Write-Host "== BankQL API"
$api = Start-Process .venv\Scripts\uvicorn -ArgumentList "bankql.api:app", "--host", "127.0.0.1", "--port", "8000" -RedirectStandardError data\logs\api.log -PassThru -NoNewWindow
$procs += $api
foreach ($i in 1..60) { try { Invoke-WebRequest http://127.0.0.1:8000/health -UseBasicParsing | Out-Null; break } catch { Start-Sleep 1 } }
Write-Host "`nDemo:    http://127.0.0.1:8000/demo`nAPI key: first value of BANKQL_API_KEYS in .env`nPress Enter to stop."
Read-Host | Out-Null
$procs | ForEach-Object { Stop-Process -Id $_.Id -ErrorAction SilentlyContinue }
& scripts\serve_local.ps1 -Stop
docker compose stop postgres redis
