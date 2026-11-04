# Full local evaluation (Windows) with both models resident; results in eval\results\, REPORT.md regenerated.
# Every runner is resumable: re-run after an interruption and finished items are skipped.
#   powershell -ExecutionPolicy Bypass -File scripts\eval_local.ps1                    # all steps
#   powershell -ExecutionPolicy Bypass -File scripts\eval_local.ps1 lookup analytics   # subset: lookup analytics chat latency report
param([string[]]$Steps = @("lookup", "analytics", "chat", "latency", "report"))
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
Get-Content .env | Where-Object { $_ -match "^[A-Z_]+=" } | ForEach-Object { $k, $v = $_ -split "=", 2; Set-Item "env:$k" $v }
$py = ".venv\Scripts\python"
$key = ($env:BANKQL_API_KEYS -split ",")[0]
$startedLlama = $false

docker compose up -d postgres redis | Out-Null
try { Invoke-WebRequest http://127.0.0.1:8081/v1/models -Headers @{Authorization = "Bearer $($env:BANKQL_LLAMA_API_KEY)"} -UseBasicParsing | Out-Null }
catch { & scripts\serve_local.ps1 -Detach; $startedLlama = $true }
try { nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader | Set-Content eval\results\gpu.txt } catch {}

try {
  foreach ($step in $Steps) {
    switch ($step) {
      "lookup" {
        Write-Host "== Phase 3: lookup eval (router = Gemma) on bird_demo"
        if (-not (Test-Path eval\datasets\lookup_bird.jsonl)) { & $py eval\make_lookup_set.py --source bird_demo --out eval\datasets\lookup_bird.jsonl }
        & $py eval\run_lookup.py --items eval\datasets\lookup_bird.jsonl --source bird_demo --out eval\results\lookup.jsonl
      }
      "analytics" {
        Write-Host "== Phase 4: BIRD financial - fast and full mode, with and without evidence"
        & $py eval\run_analytics.py --bird-dir data\bird --mode fast --evidence both
        & $py eval\run_analytics.py --bird-dir data\bird --mode full --evidence both
      }
      "chat" {
        Write-Host "== Chat smoke test through /v1/ask with the real models"
        $api = Start-Process .venv\Scripts\uvicorn -ArgumentList "bankql.api:app", "--host", "127.0.0.1", "--port", "8000" `
          -RedirectStandardError data\logs\api_eval.log -PassThru -WindowStyle Hidden
        foreach ($i in 1..60) { try { Invoke-WebRequest http://127.0.0.1:8000/health -UseBasicParsing | Out-Null; break } catch { Start-Sleep 1 } }
        try { & $py eval\chat_smoke.py --url http://127.0.0.1:8000 --key $key --source bird_demo } finally { Stop-Process -Id $api.Id -ErrorAction SilentlyContinue }
      }
      "latency" {
        Write-Host "== Latency: pipeline (FakeLLM) + real models (prefill / decode per call)"
        & $py eval\latency.py --reps 3 | Out-Null
        & $py eval\latency_real.py eval\results\chat_smoke_real.jsonl | Out-Null
      }
      "report" { Write-Host "== REPORT.md"; & $py eval\make_report.py }
      default { throw "unknown step $step" }
    }
  }
} finally { if ($startedLlama) { & scripts\serve_local.ps1 -Stop } }
Write-Host "Results in eval\results\; see REPORT.md (fill in the spot-check table by hand)."
