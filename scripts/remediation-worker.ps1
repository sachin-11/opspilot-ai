# Runs the sandbox-capable RQ worker on the host (needs Docker + the opspilot-sandbox image).
# The Compose `worker` never serves the `remediation` queue and has no Docker access, so repository
# code can only be executed by this deliberately separate worker. Do not mount the Docker socket
# into the general worker. Stop with Ctrl+C.
. "$PSScriptRoot\_common.ps1"
Import-DotEnv

Push-Location (Join-Path $Root 'services/api')
try {
    # Host-side settings come from .env (127.0.0.1 hosts and published ports).
    $env:WORKER_QUEUES = 'remediation'
    uv run python -m opspilot_api.worker.main
} finally { Pop-Location }
