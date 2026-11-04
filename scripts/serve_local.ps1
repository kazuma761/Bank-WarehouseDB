# Both llama-servers on the GPU (Windows), bound to 127.0.0.1 and requiring --api-key:
#   Gemma on :8080; Arctic-Text2SQL on :8081 with 3 parallel slots + q8_0 KV cache.
#   powershell -ExecutionPolicy Bypass -File scripts\serve_local.ps1            # foreground; Enter stops both
#   powershell -ExecutionPolicy Bypass -File scripts\serve_local.ps1 -Detach    # background (PIDs in data\logs\llama.pids)
#   powershell -ExecutionPolicy Bypass -File scripts\serve_local.ps1 -Stop
# Env/.env: LLAMA_SERVER (default llama-server.exe on PATH), BANKQL_ROUTER_GGUF, BANKQL_SQL_GGUF, BANKQL_NGL (99),
# BANKQL_FA_FLAG ("-fa on"; older builds "-fa"; needed for the quantized V cache).
param([switch]$Detach, [switch]$Stop)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force data\logs | Out-Null
$pidsFile = "data\logs\llama.pids"
if ($Stop) {
  if (Test-Path $pidsFile) { Get-Content $pidsFile | ForEach-Object { Stop-Process -Id $_ -ErrorAction SilentlyContinue }; Remove-Item $pidsFile }
  Write-Host "stopped"; exit 0
}
if (-not (Test-Path .env)) { throw "no .env - run scripts\setup_local.ps1 first" }
Get-Content .env | Where-Object { $_ -match "^[A-Z_]+=" } | ForEach-Object { $k, $v = $_ -split "=", 2; Set-Item "env:$k" $v }
if (-not $env:BANKQL_LLAMA_API_KEY) { throw "BANKQL_LLAMA_API_KEY missing in .env - run scripts\setup_local.ps1" }
$llama = if ($env:LLAMA_SERVER) { $env:LLAMA_SERVER } else { "llama-server.exe" }
function Model($role) { .venv\Scripts\python -c "import yaml;print('models/'+yaml.safe_load(open('config/models.yaml'))['$role']['hf_file'])" }
$router = if ($env:BANKQL_ROUTER_GGUF) { $env:BANKQL_ROUTER_GGUF } else { Model "router" }
$sql = if ($env:BANKQL_SQL_GGUF) { $env:BANKQL_SQL_GGUF } else { Model "sql" }
$ngl = if ($env:BANKQL_NGL) { $env:BANKQL_NGL } else { "99" }
$fa = if ($env:BANKQL_FA_FLAG) { $env:BANKQL_FA_FLAG -split " " } else { @("-fa", "on") }
foreach ($f in $router, $sql) { if (-not (Test-Path $f)) { throw "model not found: $f" } }

$common = @("--host", "127.0.0.1", "--api-key", $env:BANKQL_LLAMA_API_KEY, "-ngl", $ngl)
$p1 = Start-Process $llama -ArgumentList (@("-m", $router, "--port", "8080", "-c", "8192") + $common) `
  -RedirectStandardError data\logs\router.log -RedirectStandardOutput data\logs\router.out -PassThru -WindowStyle Hidden
$p2 = Start-Process $llama -ArgumentList (@("-m", $sql, "--port", "8081", "-c", "24576", "-np", "3",
  "--cache-type-k", "q8_0", "--cache-type-v", "q8_0") + $fa + $common) `
  -RedirectStandardError data\logs\sql.log -RedirectStandardOutput data\logs\sql.out -PassThru -WindowStyle Hidden
@($p1.Id, $p2.Id) | Set-Content $pidsFile
foreach ($s in @(@("router", 8080), @("sql", 8081))) {
  $ok = $false
  foreach ($i in 1..240) {
    try { Invoke-WebRequest "http://127.0.0.1:$($s[1])/v1/models" -Headers @{Authorization = "Bearer $($env:BANKQL_LLAMA_API_KEY)"} -UseBasicParsing | Out-Null; $ok = $true; break }
    catch { Start-Sleep 1 }
  }
  if (-not $ok) { & $PSCommandPath -Stop; throw "$($s[0]) did not come up; see data\logs\$($s[0]).log" }
  Write-Host "  $($s[0]) ready on 127.0.0.1:$($s[1])"
}
if ($Detach) { exit 0 }
Write-Host "serving - press Enter to stop"; Read-Host | Out-Null
& $PSCommandPath -Stop
