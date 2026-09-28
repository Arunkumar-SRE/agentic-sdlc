# One-time setup on Windows. Run from anywhere:
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

# Find Python 3.11+ (the "python" command, or the "py" launcher).
$python = $null
foreach ($candidate in @("python", "py")) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) {
        $ok = & $candidate -c "import sys; print(sys.version_info >= (3, 11))" 2>$null
        if ($ok -eq "True") { $python = $candidate; break }
    }
}
if (-not $python) {
    Write-Host "Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and run this again." -ForegroundColor Red
    exit 1
}
Write-Host "Using $python ($(& $python --version))" -ForegroundColor Cyan

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment in .venv ..." -ForegroundColor Cyan
    & $python -m venv .venv
    if ($LASTEXITCODE -ne 0) { exit 1 }
}
$venvPython = ".\.venv\Scripts\python.exe"

Write-Host "Installing dependencies ..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "pip install failed. On a corporate network PyPI may be blocked: try a personal network." -ForegroundColor Red
    exit 1
}

Write-Host "Running the test suite (about 20-40 seconds) ..." -ForegroundColor Cyan
& $venvPython -m pytest
if ($LASTEXITCODE -ne 0) { Write-Host "Some tests failed: copy the output above and share it." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "Setup complete. Now run any of these (independent of each other):" -ForegroundColor Green
Write-Host "  A. URL shortener demo:     powershell -ExecutionPolicy Bypass -File scripts\demo_url_shortener.ps1"
Write-Host "  B. Agentic workflow:       .\.venv\Scripts\python.exe -m agentic_sdlc run -r `"Build a scalable URL shortener service with APIs, persistence, and analytics.`""
Write-Host "  C. Golden signals report:  .\.venv\Scripts\python.exe scripts\golden_signals_report.py"
