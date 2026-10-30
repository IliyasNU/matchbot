param([string]$PythonPath)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    if ($PythonPath) {
        & $PythonPath -m venv .venv
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m venv .venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        python -m venv .venv
    } else {
        throw 'Install Python 3.12+ from python.org or pass -PythonPath C:\path\to\python.exe'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create virtual environment.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
} elseif ((Get-Item -LiteralPath '.env').Length -eq 0) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
Write-Host 'Ready. Set BOT_TOKEN and ADMIN_IDS in .env, then run .\start.ps1'
