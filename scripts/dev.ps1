param(
    [int]$Port = 5000
)

$ErrorActionPreference = "Stop"

$venvPath = ".venv"
$pythonExe = Join-Path $venvPath "Scripts\python.exe"
$pipExe = Join-Path $venvPath "Scripts\pip.exe"
$flaskExe = Join-Path $venvPath "Scripts\flask.exe"

if (-not (Test-Path $pythonExe)) {
    python -m venv $venvPath
}

& $pipExe install -r requirements.txt
& $pythonExe scripts/reconstruct_key.py

if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    Write-Host "Created .env from .env.example"
}

if (-not (Get-Command pdftoppm -ErrorAction SilentlyContinue)) {
    Write-Host "Poppler not found. Install with:"
    Write-Host "  choco install poppler"
    Write-Host "  scoop install poppler"
}

Write-Host "Starting SAGE at http://127.0.0.1:$Port"
& $flaskExe --app app run --debug --port $Port
