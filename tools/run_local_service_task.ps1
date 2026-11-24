[CmdletBinding()]
param([switch]$Start)

$ErrorActionPreference = 'Stop'
if (-not $Start) {
    throw 'This task launcher requires -Start; the Task Scheduler definition is disabled by default.'
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $env:LOCALAPPDATA 'SolanaPaperBot'
$python = Join-Path $dataDir 'venv\Scripts\python.exe'
$database = Join-Path $dataDir 'local_ledger.db'

if (-not (Test-Path $python -PathType Leaf)) {
    throw "Dedicated Python runtime not found: $python"
}
if (-not (Test-Path $database -PathType Leaf)) {
    throw "Verified local paper ledger not found: $database"
}

$env:PAPERBOT_LOCAL_DATA_DIR = $dataDir
$env:PAPERBOT_LOCAL_DB = $database
$env:PAPERBOT_LOCAL_LOCK = Join-Path $dataDir 'local.lock'
$env:PAPERBOT_LOCAL_LOG = Join-Path $dataDir 'local_monitor.log'
$env:PAPERBOT_LOCAL_DASHBOARD = Join-Path $dataDir 'local_dashboard.json'
$env:PAPERBOT_LOCAL_MIGRATION_CONFIRMED = 'YES'

& $python (Join-Path $repoRoot 'local_service.py') --db $database
exit $LASTEXITCODE
