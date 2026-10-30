# Backend quality gate. Add -Integration to also run tests needing live PostgreSQL/Redis
# (start the stack and apply migrations first; settings come from .env).
param([switch]$Integration)
. "$PSScriptRoot\_common.ps1"

Push-Location (Join-Path $Root 'services/api')
try {
    Invoke-Step 'ruff format --check' { uv run ruff format --check . }
    Invoke-Step 'ruff check' { uv run ruff check . }
    Invoke-Step 'mypy' { uv run mypy }
    Invoke-Step 'pytest (unit)' { uv run pytest }
    if ($Integration) {
        Import-DotEnv
        Invoke-Step 'pytest (integration)' { uv run pytest -m integration }
    }
} finally { Pop-Location }
