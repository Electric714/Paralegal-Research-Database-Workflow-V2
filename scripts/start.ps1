$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Runtime = Join-Path $Root '.runtime'
$UvDir = Join-Path $Runtime 'uv'
$UvExe = Join-Path $UvDir 'uv.exe'
$PythonDir = Join-Path $Runtime 'python'
$PythonBinDir = Join-Path $Runtime 'python-bin'
$VenvDir = Join-Path $Root '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$Frontend = Join-Path $Root 'frontend'
$Backend = Join-Path $Root 'backend'
$LogDir = Join-Path $Runtime 'logs'
$PidFile = Join-Path $Runtime 'server.pid'
$StdoutLog = Join-Path $LogDir 'server.log'
$StderrLog = Join-Path $LogDir 'server-error.log'
$Url = 'http://127.0.0.1:8000'
$HealthUrl = "$Url/api/health"

New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Assert-Success([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

function Test-AppOnline {
    try {
        $Health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2
        return ($Health.name -eq 'Paralegal Research Desk' -and $Health.status -eq 'ok')
    }
    catch {
        return $false
    }
}

function Stop-ExistingApp {
    $StoppedProcessIds = @()

    if (Test-Path $PidFile) {
        $SavedPid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($SavedPid -and $SavedPid -match '^\d+$') {
            $SavedProcess = Get-Process -Id ([int]$SavedPid) -ErrorAction SilentlyContinue
            if ($SavedProcess) {
                Write-Host 'Stopping existing Paralegal Research Desk backend so current code is loaded...'
                Stop-Process -Id $SavedProcess.Id -Force -ErrorAction SilentlyContinue
                $StoppedProcessIds += $SavedProcess.Id
                try { Wait-Process -Id $SavedProcess.Id -Timeout 5 -ErrorAction SilentlyContinue } catch {}
            }
        }
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }

    # Older launcher versions could leave a healthy backend running without a usable
    # PID file. If this port is still serving our app, stop that listener as well.
    if (Test-AppOnline) {
        try {
            $Listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction Stop | Select-Object -First 1
            if ($Listener -and $Listener.OwningProcess -and ($StoppedProcessIds -notcontains [int]$Listener.OwningProcess)) {
                $ListenerProcess = Get-Process -Id ([int]$Listener.OwningProcess) -ErrorAction SilentlyContinue
                if ($ListenerProcess) {
                    Write-Host 'Stopping stale backend listener on port 8000...'
                    Stop-Process -Id $ListenerProcess.Id -Force -ErrorAction SilentlyContinue
                    try { Wait-Process -Id $ListenerProcess.Id -Timeout 5 -ErrorAction SilentlyContinue } catch {}
                }
            }
        }
        catch {
            # If Get-NetTCPConnection is unavailable, the health check below will
            # fail closed rather than silently reusing stale backend state.
        }
    }

    $Deadline = (Get-Date).AddSeconds(8)
    while ((Get-Date) -lt $Deadline -and (Test-AppOnline)) {
        Start-Sleep -Milliseconds 250
    }

    if (Test-AppOnline) {
        throw 'An older Paralegal Research Desk backend is still running on port 8000. Close it and run START_HERE.bat again.'
    }
}

Write-Host ''
Write-Host '==============================================='
Write-Host ' Paralegal Research Desk'
Write-Host '==============================================='
Write-Host ''

if (-not (Test-Path $UvExe)) {
    Write-Host '[1/7] Preparing local runtime tools...'
    New-Item -ItemType Directory -Force -Path $UvDir | Out-Null
    $env:UV_UNMANAGED_INSTALL = $UvDir
    $env:UV_NO_MODIFY_PATH = '1'
    try {
        Invoke-RestMethod 'https://astral.sh/uv/install.ps1' | Invoke-Expression
    }
    finally {
        Remove-Item Env:UV_UNMANAGED_INSTALL -ErrorAction SilentlyContinue
        Remove-Item Env:UV_NO_MODIFY_PATH -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path $UvExe)) {
        throw 'Could not install the local runtime bootstrap tool.'
    }
} else {
    Write-Host '[1/7] Local runtime tools ready.'
}

$env:UV_PYTHON_INSTALL_DIR = $PythonDir
$env:UV_PYTHON_BIN_DIR = $PythonBinDir
$env:UV_PYTHON_NO_REGISTRY = '1'
$env:UV_MANAGED_PYTHON = '1'

if (-not (Test-Path $VenvPython)) {
    Write-Host '[2/7] Downloading project-local Python and creating isolated environment...'
    & $UvExe python install 3.12 --managed-python
    Assert-Success 'Python download'
    & $UvExe venv $VenvDir --python 3.12 --managed-python
    Assert-Success 'Virtual environment creation'
} else {
    Write-Host '[2/7] Project-local Python ready.'
}

Write-Host '[3/7] Preparing backend dependencies...'
& $UvExe pip install --python $VenvPython -r (Join-Path $Backend 'requirements.txt') --quiet
Assert-Success 'Backend dependency installation'

$NodeVersion = '22.22.1'
$NodeArch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x64' }
$NodeFolder = "node-v$NodeVersion-win-$NodeArch"
$NodeHome = Join-Path $Runtime $NodeFolder
$NodeExe = Join-Path $NodeHome 'node.exe'
$NpmExe = Join-Path $NodeHome 'npm.cmd'

if (-not (Test-Path $NodeExe)) {
    Write-Host '[4/7] Downloading portable frontend runtime...'
    $NodeZip = Join-Path $Runtime "$NodeFolder.zip"
    $NodeUrl = "https://nodejs.org/dist/v$NodeVersion/$NodeFolder.zip"
    Invoke-WebRequest -UseBasicParsing -Uri $NodeUrl -OutFile $NodeZip
    Expand-Archive -Path $NodeZip -DestinationPath $Runtime -Force
    Remove-Item $NodeZip -Force
    if (-not (Test-Path $NodeExe)) {
        throw 'Portable frontend runtime download completed, but node.exe was not found.'
    }
} else {
    Write-Host '[4/7] Portable frontend runtime ready.'
}

$env:PATH = "$NodeHome;$env:PATH"

Write-Host '[5/7] Preparing frontend dependencies...'
Push-Location $Frontend
try {
    & $NpmExe install --no-audit --no-fund --silent
    Assert-Success 'Frontend dependency installation'

    Write-Host '[6/7] Building application interface...'
    & $NpmExe run build
    Assert-Success 'Frontend build'
}
finally {
    Pop-Location
}

Write-Host '[7/7] Starting application...'

# Always restart the project backend after rebuilding. Reusing an existing Python
# process leaves module-level source metadata cached in memory and can make the
# Research page show sources as not implemented even after those adapters were merged.
Stop-ExistingApp

$StartArgs = @{
    FilePath = $VenvPython
    ArgumentList = @('-m', 'uvicorn', 'app.main_with_wcca:app', '--host', '127.0.0.1', '--port', '8000')
    WorkingDirectory = $Backend
    WindowStyle = 'Hidden'
    RedirectStandardOutput = $StdoutLog
    RedirectStandardError = $StderrLog
    PassThru = $true
}
$Process = Start-Process @StartArgs
Set-Content -Path $PidFile -Value $Process.Id -Encoding ascii

$Deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $Deadline) {
    if (Test-AppOnline) { break }
    if ($Process.HasExited) {
        $ErrorTail = if (Test-Path $StderrLog) { (Get-Content $StderrLog -Tail 25) -join [Environment]::NewLine } else { 'No server error log was created.' }
        throw "The application server stopped during startup.`n`n$ErrorTail"
    }
    Start-Sleep -Milliseconds 500
}

if (-not (Test-AppOnline)) {
    throw "The application did not respond in time. Check: $StderrLog"
}

Start-Process $Url

Write-Host ''
Write-Host 'Paralegal Research Desk is open in your browser.'
Write-Host 'This launcher can now close; the application continues running quietly in the background.'
Write-Host 'Use STOP_HERE.bat when you want to shut it down.'
Start-Sleep -Seconds 2
