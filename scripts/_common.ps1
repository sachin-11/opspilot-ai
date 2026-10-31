# Shared helpers, dot-sourced by the other scripts.
$ErrorActionPreference = 'Stop'
$script:Root = Split-Path -Parent $PSScriptRoot

function Import-DotEnv {
    param([string]$Path = (Join-Path $script:Root '.env'))
    if (-not (Test-Path $Path)) {
        throw "Missing $Path. Run scripts/bootstrap.ps1 first (it copies .env.example to .env)."
    }
    foreach ($line in Get-Content $Path) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2].Trim()
        }
    }
}

function Invoke-Step {
    param([string]$Title, [scriptblock]$Action)
    Write-Host "`n==> $Title" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "Step failed: $Title (exit code $LASTEXITCODE)" }
}

# pnpm is provided through Corepack (bundled with Node); nothing is installed globally.
function Invoke-Pnpm {
    corepack pnpm @args
}

# The postgres container of the compose project, unless a container name or ID is given.
function Resolve-PostgresContainer {
    param([string]$Name = '')
    if ($Name) { return $Name }
    $id = docker compose ps -q postgres | Select-Object -First 1
    if (-not $id) { throw 'The compose postgres service is not running (docker compose up -d postgres); or pass -Container.' }
    return $id.Trim()
}
