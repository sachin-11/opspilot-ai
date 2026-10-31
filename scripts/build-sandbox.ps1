# Builds the hardened verification sandbox image used by remediation (Module 6).
. "$PSScriptRoot\_common.ps1"

Invoke-Step 'docker build opspilot-sandbox' {
    docker build -f (Join-Path $Root 'infra/docker/sandbox.Dockerfile') -t opspilot-sandbox:latest (Join-Path $Root 'infra/docker')
}
