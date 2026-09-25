param([string[]]$LiveSource = @())
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $PythonExe)) {
    throw 'Run scripts\start.ps1 -SetupOnly first.'
}
$CheckArgs = @((Join-Path $PSScriptRoot 'check.py'))
foreach ($Source in $LiveSource) { $CheckArgs += @('--live-source', $Source) }
& $PythonExe @CheckArgs
exit $LASTEXITCODE
