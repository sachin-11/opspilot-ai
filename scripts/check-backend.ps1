# Backend quality gate. Add -Integration to also run tests needing live PostgreSQL/Redis
# (start the stack and apply migrations first; settings come from .env).
param([switch]$Integration)
. "$PSScriptRoot\_common.ps1"
# Integration also needs the sandbox image: scripts/build-sandbox.ps1

Push-Location (Join-Path $Root 'services/api')
try {
    Invoke-Step 'ruff format --check' { uv run ruff format --check . }
    Invoke-Step 'ruff check' { uv run ruff check . }
    Invoke-Step 'mypy' { uv run mypy }
    Invoke-Step 'pytest (unit)' { uv run pytest }
    if ($Integration) {
        Import-DotEnv
        Invoke-Step 'pytest (integration)' { uv run pytest -m integration }
        Invoke-Step 'knowledge evaluation' { uv run python -m opspilot_api.knowledge.evaluation --dataset tests/fixtures/retrieval_evaluation.json }
        Invoke-Step 'repository evaluation' { uv run python -m opspilot_api.repositories.evaluation --dataset tests/fixtures/repository_evaluation.json }
        Invoke-Step 'remediation evaluation (fake sandbox)' { uv run python -m opspilot_api.remediation.evaluation --dataset tests/fixtures/remediation_evaluation.json --sandbox fake }
        Invoke-Step 'remediation evaluation (docker sandbox)' { uv run python -m opspilot_api.remediation.evaluation --dataset tests/fixtures/remediation_evaluation.json --sandbox docker }
        Invoke-Step 'pipeline smoke test' { uv run python -m opspilot_api.observability.smoke }
    }
} finally { Pop-Location }
