# Starts the full local stack in Docker, waits for health, and applies migrations.
# Use -Down to stop it (data volumes are kept).
param([switch]$Down)
. "$PSScriptRoot\_common.ps1"
Set-Location $Root

if ($Down) {
    docker compose down
    exit $LASTEXITCODE
}

Import-DotEnv
Invoke-Step 'Build and start services (waits for health checks)' { docker compose up -d --build --wait }
Invoke-Step 'Apply database migrations' { docker compose exec api alembic upgrade head }

Write-Host "`nWeb:        http://localhost:$($env:WEB_HOST_PORT)"
Write-Host "API docs:   http://localhost:$($env:API_HOST_PORT)/docs"
Write-Host "Readiness:  http://localhost:$($env:API_HOST_PORT)/health/ready"
