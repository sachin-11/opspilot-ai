# Applies Alembic migrations inside the running api container.
. "$PSScriptRoot\_common.ps1"
Set-Location $Root
Import-DotEnv
Invoke-Step 'alembic upgrade head' { docker compose exec api alembic upgrade head }
