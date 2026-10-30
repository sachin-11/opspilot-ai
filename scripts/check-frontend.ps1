# Frontend quality gate: lint, type-check, tests, production build.
. "$PSScriptRoot\_common.ps1"

Push-Location (Join-Path $Root 'apps/web')
try {
    if (-not $env:NEXT_PUBLIC_API_BASE_URL) {
        $env:NEXT_PUBLIC_API_BASE_URL = 'http://localhost:8300'  # build-time placeholder; no secret
    }
    Invoke-Step 'eslint' { Invoke-Pnpm lint }
    Invoke-Step 'tsc --noEmit' { Invoke-Pnpm typecheck }
    Invoke-Step 'vitest' { Invoke-Pnpm test }
    Invoke-Step 'next build' { Invoke-Pnpm build }
} finally { Pop-Location }
