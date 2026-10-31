# Restores a backup made by scripts/backup.ps1.
#
# Safe by default: restores into a NEW database and never touches the live one.
#   scripts/restore.ps1 -File backups\opspilot-....dump -Verify
#       restore into a temporary database, run sanity checks, drop it (a restore drill)
#   scripts/restore.ps1 -File backups\opspilot-....dump
#       restore into <database>_restore_<timestamp> for inspection
#   scripts/restore.ps1 -File ... -ReplaceLive -ConfirmDatabaseName opspilot
#       replace the live database. Stop api, worker and consumers first; refuses if anything is
#       still connected, and the confirmation must equal the database name.
param(
    [Parameter(Mandatory = $true)][string]$File,
    [string]$Container = '',
    [string]$User = '',
    [string]$Database = '',
    [switch]$Verify,
    [switch]$ReplaceLive,
    [string]$ConfirmDatabaseName = ''
)
. "$PSScriptRoot\_common.ps1"
Set-Location $Root
Import-DotEnv
if (-not $User) { $User = $env:POSTGRES_USER }
if (-not $Database) { $Database = $env:POSTGRES_DB }
$Container = Resolve-PostgresContainer $Container
if (-not (Test-Path $File)) { throw "Backup file not found: $File" }
$File = (Resolve-Path $File).Path

if ($ReplaceLive -and $Verify) { throw '-Verify and -ReplaceLive are mutually exclusive.' }
if ($ReplaceLive -and $ConfirmDatabaseName -ne $Database) {
    throw "Refusing to replace '$Database': pass -ConfirmDatabaseName $Database to confirm."
}

# 1. Integrity: the checksum written at backup time must still match.
$sumFile = "$File.sha256"
if (Test-Path $sumFile) {
    $expected = ((Get-Content $sumFile -Raw).Trim() -split '\s+')[0].ToLower()
    $actual = (Get-FileHash -Algorithm SHA256 -Path $File).Hash.ToLower()
    if ($expected -ne $actual) { throw "Checksum mismatch for $File; the backup is corrupt or was modified." }
    Write-Host 'Checksum OK' -ForegroundColor Green
} else {
    Write-Warning "No $([IO.Path]::GetFileName($sumFile)) next to the backup; integrity cannot be checked."
}

function Invoke-Psql {
    param([string]$Db, [string]$Sql)
    $out = docker exec $Container psql -U $User -d $Db -v ON_ERROR_STOP=1 -t -A -c $Sql
    if ($LASTEXITCODE -ne 0) { throw "psql failed: $Sql" }
    return ($out | Out-String).Trim()
}

$stamp = Get-Date -Format 'yyyyMMddHHmmss'
if ($ReplaceLive) { $target = $Database } else { $target = "${Database}_restore_$stamp" }
$remote = "/tmp/restore-$stamp.dump"

Invoke-Step 'copy the backup into the container' { docker cp $File "${Container}:$remote" }
try {
    if ($ReplaceLive) {
        $others = Invoke-Psql 'postgres' "SELECT count(*) FROM pg_stat_activity WHERE datname = '$Database' AND pid <> pg_backend_pid()"
        if ([int]$others -gt 0) { throw "$others session(s) are still connected to '$Database'. Stop api, worker and consumers first." }
        Invoke-Step "drop and recreate $Database" {
            docker exec $Container psql -U $User -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE `"$Database`"" -c "CREATE DATABASE `"$Database`""
        }
    } else {
        Invoke-Step "create $target" { docker exec $Container psql -U $User -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE `"$target`"" }
    }
    # pgvector is created by migration 0001 and travels with the dump; the extension must be
    # installable in the server image, which the project's pgvector image is.
    Invoke-Step "pg_restore into $target" {
        docker exec $Container pg_restore -U $User -d $target --no-owner --exit-on-error $remote
    }

    $version = Invoke-Psql $target 'SELECT version_num FROM alembic_version'
    $tables = Invoke-Psql $target "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
    if (-not $version) { throw 'Restored database has no alembic_version row; the backup is not an OpsPilot database.' }
    Write-Host "Restored schema revision $version with $tables public tables." -ForegroundColor Green
    foreach ($table in @('workspaces', 'incidents', 'users', 'audit_events')) {
        $exists = Invoke-Psql $target "SELECT to_regclass('public.$table') IS NOT NULL"
        if ($exists -eq 't') { Write-Host ("  {0,-14} {1} rows" -f $table, (Invoke-Psql $target "SELECT count(*) FROM $table")) }
    }
} finally {
    docker exec $Container rm -f $remote | Out-Null
}

if ($Verify) {
    Invoke-Step "drop the temporary database $target" {
        docker exec $Container psql -U $User -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE `"$target`""
    }
    Write-Host "`nRestore drill passed; temporary database removed." -ForegroundColor Green
} elseif (-not $ReplaceLive) {
    Write-Host "`nRestored into database '$target'. Inspect it, then drop it when finished:" -ForegroundColor Yellow
    Write-Host "  docker exec $Container psql -U $User -d postgres -c 'DROP DATABASE ""$target""'"
} else {
    Write-Host "`nLive database '$Database' replaced. Start api, worker and consumers again." -ForegroundColor Yellow
}
