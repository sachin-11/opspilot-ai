# Runs every backend and frontend check. Pass -Integration for live-service tests.
param([switch]$Integration)
$ErrorActionPreference = 'Stop'
& "$PSScriptRoot\check-backend.ps1" -Integration:$Integration
& "$PSScriptRoot\check-frontend.ps1"
Write-Host "`nAll checks passed." -ForegroundColor Green
