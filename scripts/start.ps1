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

New-Item -ItemType Directory -Force -Path $Runtime | Out-Null

function Assert-Success([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

Write-Host ''
Write-Host '==============================================='
Write-Host ' Paralegal Research Desk - Local Setup/Launch'
Write-Host '==============================================='
Write-Host ''

# Bootstrap uv locally. uv itself does not require Python.
if (-not (Test-Path $UvExe)) {
    Write-Host '[1/6] Downloading the local Python bootstrap tool...'
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
        throw 'Could not install the local uv bootstrap tool.'
    }
} else {
    Write-Host '[1/6] Local bootstrap tool already available.'
}

# Keep uv-managed Python completely inside this project.
$env:UV_PYTHON_INSTALL_DIR = $PythonDir
$env:UV_PYTHON_BIN_DIR = $PythonBinDir
$env:UV_PYTHON_NO_REGISTRY = '1'
$env:UV_MANAGED_PYTHON = '1'

if (-not (Test-Path $VenvPython)) {
    Write-Host '[2/6] Downloading project-local Python and creating .venv...'
    & $UvExe python install 3.12 --managed-python
    Assert-Success 'Python download'
    & $UvExe venv $VenvDir --python 3.12 --managed-python
    Assert-Success 'Virtual environment creation'
} else {
    Write-Host '[2/6] Project Python environment already available.'
}

Write-Host '[3/6] Installing/updating backend dependencies inside .venv...'
& $UvExe pip install --python $VenvPython -r (Join-Path $Root 'backend\requirements.txt')
Assert-Success 'Backend dependency installation'

# Bootstrap a portable Node.js runtime locally instead of requiring a system install.
$NodeVersion = '22.22.1'
$NodeArch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x64' }
$NodeFolder = "node-v$NodeVersion-win-$NodeArch"
$NodeHome = Join-Path $Runtime $NodeFolder
$NodeExe = Join-Path $NodeHome 'node.exe'
$NpmExe = Join-Path $NodeHome 'npm.cmd'

if (-not (Test-Path $NodeExe)) {
    Write-Host '[4/6] Downloading portable Node.js...'
    $NodeZip = Join-Path $Runtime "$NodeFolder.zip"
    $NodeUrl = "https://nodejs.org/dist/v$NodeVersion/$NodeFolder.zip"
    Invoke-WebRequest -UseBasicParsing -Uri $NodeUrl -OutFile $NodeZip
    Expand-Archive -Path $NodeZip -DestinationPath $Runtime -Force
    Remove-Item $NodeZip -Force
    if (-not (Test-Path $NodeExe)) {
        throw 'Portable Node.js download completed, but node.exe was not found.'
    }
} else {
    Write-Host '[4/6] Portable Node.js already available.'
}

# npm lifecycle scripts need the portable node.exe on PATH.
$env:PATH = "$NodeHome;$env:PATH"

Write-Host '[5/6] Installing/updating frontend dependencies...'
Push-Location $Frontend
try {
    & $NpmExe install --no-audit --no-fund
    Assert-Success 'Frontend dependency installation'
}
finally {
    Pop-Location
}

Write-Host '[6/6] Starting the application...'
$BackendCommand = "Set-Location '$Root\backend'; & '$VenvPython' -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"
$FrontendCommand = "`$env:PATH='$NodeHome;' + `$env:PATH; Set-Location '$Frontend'; & '$NpmExe' run dev"

Start-Process powershell.exe -ArgumentList '-NoExit','-NoProfile','-ExecutionPolicy','Bypass','-Command',$BackendCommand
Start-Process powershell.exe -ArgumentList '-NoExit','-NoProfile','-ExecutionPolicy','Bypass','-Command',$FrontendCommand

Write-Host 'Waiting for the local web application to respond...'
$Url = 'http://127.0.0.1:5173'
$Deadline = (Get-Date).AddSeconds(60)
$Online = $false
while ((Get-Date) -lt $Deadline) {
    try {
        $Response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
        if ($Response.StatusCode -ge 200 -and $Response.StatusCode -lt 500) {
            $Online = $true
            break
        }
    }
    catch {
        Start-Sleep -Milliseconds 750
    }
}

if (-not $Online) {
    throw 'The local servers were started, but the web application did not respond. Check the Backend and Frontend windows for the specific error.'
}

Start-Process $Url
Write-Host ''
Write-Host 'Paralegal Research Desk is running.'
Write-Host 'Browser: http://127.0.0.1:5173'
Write-Host 'Backend: http://127.0.0.1:8000'
Write-Host ''
Write-Host 'Keep the Backend and Frontend PowerShell windows open while using the program.'
