$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI is required. Install it from https://aka.ms/installazurecliwindows."
}
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python 3 is required. Install it and reopen this terminal."
}

Write-Host "Starting Fabric Tenant Settings Governance..."
Write-Host "The dashboard will open at http://127.0.0.1:8000"
Write-Host "Keep this window open. Press Ctrl+C to stop the application."

python .\fabric_tenant_settings_server.py
if ($LASTEXITCODE -ne 0) {
    throw "The localhost application stopped with exit code $LASTEXITCODE."
}
