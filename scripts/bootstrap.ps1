# Installs project dependencies (locked) and creates a local .env if missing.
. "$PSScriptRoot\_common.ps1"

foreach ($tool in 'node', 'uv', 'docker') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "Required tool not found on PATH: $tool" }
}

$envFile = Join-Path $Root '.env'
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $Root '.env.example') $envFile
    Write-Host "Created .env from .env.example (local-only defaults)."
} else {
    Write-Host ".env already exists; leaving it untouched."
}

Invoke-Step 'Install backend dependencies (uv sync --frozen)' {
    Push-Location (Join-Path $Root 'services/api'); try { uv sync --frozen } finally { Pop-Location }
}
Invoke-Step 'Install frontend dependencies (pnpm install --frozen-lockfile)' {
    Push-Location (Join-Path $Root 'apps/web'); try { Invoke-Pnpm install --frozen-lockfile } finally { Pop-Location }
}
Write-Host "`nBootstrap complete. Next: scripts/dev.ps1" -ForegroundColor Green
