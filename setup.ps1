# Ligxt Video Editor - full environment setup.
# Run from PowerShell:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
# Safe to re-run. Uses $LASTEXITCODE checks (not $ErrorActionPreference="Stop",
# which makes PowerShell 5.1 treat pip's stderr warnings as fatal).

$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
Set-Location $Root

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "`nSETUP FAILED: $msg" -ForegroundColor Red; exit 1 }

Step "Checking prerequisites"
py -3.11 --version
if ($LASTEXITCODE -ne 0) { Fail "Python 3.11 not found via 'py -3.11'. Install Python 3.11 from python.org (keep your 3.13 default)." }
# Capture output first: piping a native exe into Select-Object -First stops it early and
# leaves a non-zero $LASTEXITCODE in PowerShell 5.1.
$out = ffmpeg -hide_banner -version 2>$null
if ($LASTEXITCODE -ne 0) { Fail "ffmpeg not found on PATH." }
Write-Host ($out | Select-Object -First 1)
$out = ffprobe -hide_banner -version 2>$null
if ($LASTEXITCODE -ne 0) { Fail "ffprobe not found on PATH." }
Write-Host ($out | Select-Object -First 1)
node --version
if ($LASTEXITCODE -ne 0) { Fail "Node.js not found. Install Node 20 LTS or newer." }

Step "Creating folders"
foreach ($d in @("templates", "projects", "media", "exports", "cache")) {
    if (-not (Test-Path (Join-Path $Root $d))) { New-Item -ItemType Directory -Path (Join-Path $Root $d) | Out-Null }
}

Step "Creating Python 3.11 virtual environment (.venv)"
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    py -3.11 -m venv (Join-Path $Root ".venv")
    if ($LASTEXITCODE -ne 0) { Fail "Could not create the virtual environment." }
} else {
    Write-Host ".venv already exists, reusing it."
}

Step "Installing backend packages"
& $VenvPy -m pip install --upgrade pip --timeout 60 --retries 10
if ($LASTEXITCODE -ne 0) { Fail "pip upgrade failed (check your internet connection)." }
& $VenvPy -m pip install -r (Join-Path $Root "backend\requirements.txt") --timeout 60 --retries 10
if ($LASTEXITCODE -ne 0) { Fail "Installing backend requirements failed." }

Step "Installing frontend packages"
Push-Location (Join-Path $Root "frontend")
npm install --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { Pop-Location; Fail "npm install failed." }

Step "Building frontend"
npm run build
if ($LASTEXITCODE -ne 0) { Pop-Location; Fail "Frontend build failed." }
Pop-Location

Step "Checking GPU encoder (h264_nvenc)"
ffmpeg -hide_banner -loglevel error -f lavfi -i color=s=256x256:d=0.2 -c:v h264_nvenc -f null - 2>$null
if ($LASTEXITCODE -eq 0) { Write-Host "NVENC available: drafts and proxies will use the GPU." -ForegroundColor Green }
else { Write-Host "NVENC not available: falling back to CPU (libx264). Everything still works, just slower." -ForegroundColor Yellow }

Step "Desktop shortcut"
& (Join-Path $Root "install_shortcut.ps1")

Write-Host "`nSetup complete. Start the editor from the 'Ligxt Video Editor' desktop shortcut (tray icon: Open / Stop)," -ForegroundColor Green
Write-Host "or from a terminal with  .\start.ps1  (PowerShell) /  ./start.sh  (Git Bash)." -ForegroundColor Green
