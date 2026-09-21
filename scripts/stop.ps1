$ErrorActionPreference = 'SilentlyContinue'

$Root = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root '.runtime\server.pid'

if (-not (Test-Path $PidFile)) {
    Write-Host 'Paralegal Research Desk is not currently running.'
    exit 0
}

$ServerPid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
if ($ServerPid -and $ServerPid -match '^\d+$') {
    $Process = Get-Process -Id ([int]$ServerPid) -ErrorAction SilentlyContinue
    if ($Process) {
        Stop-Process -Id $Process.Id -Force
        Write-Host 'Paralegal Research Desk stopped.'
    } else {
        Write-Host 'The saved server process is no longer running.'
    }
}

Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
