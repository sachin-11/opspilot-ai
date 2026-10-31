# Logical PostgreSQL backup (pg_dump custom format) into backups/ (git-ignored).
#
# Covers every application table, the LangGraph checkpoints, and the pgvector data. It does NOT
# cover the knowledge_data volume (uploaded documents) or Redis (queue and rate-limit state, not a
# system of record); see docs/runbook-backup-restore.md.
#
#   scripts/backup.ps1                       # uses the compose postgres service and .env
#   scripts/backup.ps1 -Container mydb -User app -Database opspilot -OutDir D:\backups
param(
    [string]$Container = '',
    [string]$User = '',
    [string]$Database = '',
    [string]$OutDir = ''
)
. "$PSScriptRoot\_common.ps1"
Set-Location $Root
Import-DotEnv
if (-not $User) { $User = $env:POSTGRES_USER }
if (-not $Database) { $Database = $env:POSTGRES_DB }
if (-not $OutDir) { $OutDir = Join-Path $Root 'backups' }
$Container = Resolve-PostgresContainer $Container
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$name = "opspilot-$(Get-Date -Format 'yyyyMMdd-HHmmss').dump"
$remote = "/tmp/$name"
$local = Join-Path $OutDir $name

Invoke-Step "pg_dump $Database" { docker exec $Container pg_dump -U $User -d $Database -Fc --no-owner -f $remote }
try {
    # An archive that cannot be listed cannot be restored; fail now, not during an incident.
    Invoke-Step 'verify the archive is readable' { docker exec $Container pg_restore --list $remote | Out-Null }
    Invoke-Step 'copy out of the container' { docker cp "${Container}:$remote" $local }
} finally {
    docker exec $Container rm -f $remote | Out-Null
}

$hash = (Get-FileHash -Algorithm SHA256 -Path $local).Hash.ToLower()
Set-Content -Path "$local.sha256" -Value "$hash  $name" -Encoding ascii
$size = '{0:N1} KiB' -f ((Get-Item $local).Length / 1KB)
Write-Host "`nBackup written: $local ($size)" -ForegroundColor Green
Write-Host "SHA-256:        $hash"
Write-Host 'Store a copy off this machine; a backup on the same disk is not a backup.'
