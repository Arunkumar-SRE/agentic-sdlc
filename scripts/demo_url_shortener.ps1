# Starts the URL shortener, exercises every endpoint, then stops it. Windows PowerShell 5.1 or 7+.
#   powershell -ExecutionPolicy Bypass -File scripts\demo_url_shortener.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
$service = Join-Path $root "targets\url_shortener"
$base = "http://127.0.0.1:8000"

# Refuse to run against a service that is already on port 8000 (its numbers would not match this demo).
try {
    Invoke-RestMethod "$base/health" -TimeoutSec 2 | Out-Null
    Write-Host "Something is already running on port 8000 (probably an earlier uvicorn)." -ForegroundColor Red
    Write-Host "Stop it first: press Ctrl+C in that PowerShell window, then run this script again." -ForegroundColor Red
    exit 1
} catch { }

Remove-Item (Join-Path $service "demo.db") -ErrorAction SilentlyContinue
$env:DATABASE_URL = "sqlite:///./demo.db"
$server = Start-Process -FilePath $python -ArgumentList "-m", "uvicorn", "app.main:app", "--port", "8000" `
    -WorkingDirectory $service -PassThru -WindowStyle Hidden

function Status-Of([string]$Method, [string]$Uri, [string]$Body) {
    try {
        if ($Body) { Invoke-RestMethod -Method $Method -Uri $Uri -ContentType "application/json" -Body $Body | Out-Null }
        else { Invoke-RestMethod -Method $Method -Uri $Uri | Out-Null }
        return 200
    } catch { return [int]$_.Exception.Response.StatusCode }
}

try {
    $up = $false
    for ($i = 0; $i -lt 40; $i++) {
        try { Invoke-RestMethod "$base/health" | Out-Null; $up = $true; break } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $up) { throw "The service did not start on port 8000 (is something else using it?)" }

    Write-Host "`n1. Create a short link (POST /api/v1/urls)" -ForegroundColor Cyan
    $body = @{ long_url = "https://example.com/some/very/long/path?utm=demo"; custom_alias = "demo1" } | ConvertTo-Json
    Invoke-RestMethod -Method Post -Uri "$base/api/v1/urls" -ContentType "application/json" -Body $body | Format-List

    Write-Host "2. Follow the short link three times (GET /demo1 -> 302 redirect, click recorded)" -ForegroundColor Cyan
    foreach ($n in 1..3) {
        curl.exe -s -o NUL -w "   HTTP %{http_code} -> %{redirect_url}`n" -H "Referer: https://news.example" "$base/demo1"
    }

    Write-Host "`n3. Analytics (GET /api/v1/urls/demo1/analytics)" -ForegroundColor Cyan
    Invoke-RestMethod "$base/api/v1/urls/demo1/analytics" | ConvertTo-Json -Depth 5

    Write-Host "`n4. Error handling" -ForegroundColor Cyan
    Write-Host "   Same alias again        -> $(Status-Of 'Post' "$base/api/v1/urls" $body) (expect 409 Conflict)"
    $bad = @{ long_url = "not-a-url" } | ConvertTo-Json
    Write-Host "   Invalid URL             -> $(Status-Of 'Post' "$base/api/v1/urls" $bad) (expect 422)"
    $reserved = @{ long_url = "https://example.com"; custom_alias = "docs" } | ConvertTo-Json
    Write-Host "   Reserved alias 'docs'   -> $(Status-Of 'Post' "$base/api/v1/urls" $reserved) (expect 422)"
    Write-Host "   Unknown short code      -> $(Status-Of 'Get' "$base/nope123") (expect 404)"

    Write-Host "`n5. Operations endpoints" -ForegroundColor Cyan
    Write-Host "   /health -> $((Invoke-RestMethod "$base/health").status)"
    Write-Host "   /ready  -> $((Invoke-RestMethod "$base/ready").status)"
    Write-Host "   /metrics (first lines):"
    (Invoke-RestMethod "$base/metrics") -split "`n" | Select-Object -First 6 | ForEach-Object { "     $_" }

    Write-Host "`n6. Golden signals (GET /ops/golden-signals)" -ForegroundColor Cyan
    $g = Invoke-RestMethod "$base/ops/golden-signals?window=300"
    Write-Host "   Latency    redirect p99 $($g.latency.redirect.p99_ms) ms (p50 $($g.latency.redirect.p50_ms) ms)"
    Write-Host "   Traffic    $($g.traffic.requests) requests, $($g.traffic.rps) req/s"
    Write-Host "   Errors     5xx $($g.errors.server_error_rate_pct)%  |  4xx $($g.errors.'4xx') (client errors, expected)"
    Write-Host "   Saturation peak in flight $($g.saturation.in_flight_peak)"
    foreach ($s in $g.slos) { Write-Host "   SLO $($s.status.PadRight(8)) $($s.slo)" }

    Write-Host "`nLive dashboard: $base/ops/dashboard   |   API docs: $base/docs" -ForegroundColor Green
    Read-Host "Press Enter to stop the service"
} finally {
    Stop-Process -Id $server.Id -ErrorAction SilentlyContinue
}
