# Ligxt Video Editor - launcher. Starts the backend (which serves the built UI) and opens the browser.
# Usage:  .\start.ps1            (normal)
#         .\start.ps1 -Port 9000 (different port)
#         .\start.ps1 -NoBrowser
param(
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
Set-Location $Root
$Url = "http://127.0.0.1:$Port"

$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Host "The Python environment is missing. Run .\setup.ps1 first." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path (Join-Path $Root "frontend\dist\index.html"))) {
    Write-Host "The UI has not been built. Run .\setup.ps1 (or: cd frontend; npm run build)." -ForegroundColor Red
    exit 1
}

# Already running? Just open the browser.
try {
    $r = Invoke-WebRequest -Uri "$Url/api/health" -UseBasicParsing -TimeoutSec 2
    if ($r.StatusCode -eq 200) {
        Write-Host "Ligxt is already running at $Url"
        if (-not $NoBrowser) { Start-Process $Url }
        exit 0
    }
} catch { }

if (-not $NoBrowser) { $env:LIGXT_OPEN_BROWSER = "1" } else { $env:LIGXT_OPEN_BROWSER = "0" }
$env:LIGXT_PORT = "$Port"

Write-Host "Starting Ligxt Video Editor at $Url  (press Ctrl+C to stop)" -ForegroundColor Cyan
& $VenvPy -m uvicorn app.main:app --app-dir (Join-Path $Root "backend") --host 127.0.0.1 --port $Port --log-level warning
if ($LASTEXITCODE -ne 0) {
    Write-Host "The server stopped with an error (exit code $LASTEXITCODE). If the port is in use, try: .\start.ps1 -Port 9000" -ForegroundColor Red
}
