$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) { return @('py', '-3') }
    if (Get-Command python -ErrorAction SilentlyContinue) { return @('python') }
    throw 'Python 3 was not found. Install Python 3, then run START_HERE.bat again.'
}

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw 'Node.js/npm was not found. Install the current Node.js LTS release, then run START_HERE.bat again.'
}

$PythonCmd = Find-Python
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'

if (-not (Test-Path $VenvPython)) {
    Write-Host 'Creating Python virtual environment...'
    if ($PythonCmd.Count -eq 2) {
        & $PythonCmd[0] $PythonCmd[1] -m venv (Join-Path $Root '.venv')
    } else {
        & $PythonCmd[0] -m venv (Join-Path $Root '.venv')
    }
}

Write-Host 'Installing/updating backend dependencies...'
& $VenvPython -m pip install -r (Join-Path $Root 'backend\requirements.txt')

$Frontend = Join-Path $Root 'frontend'
if (-not (Test-Path (Join-Path $Frontend 'node_modules'))) {
    Write-Host 'Installing frontend dependencies...'
    Push-Location $Frontend
    try { npm install } finally { Pop-Location }
}

$BackendCommand = "Set-Location '$Root\backend'; & '$VenvPython' -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"
$FrontendCommand = "Set-Location '$Frontend'; npm run dev"

Write-Host 'Starting backend on http://127.0.0.1:8000 ...'
Start-Process powershell.exe -ArgumentList '-NoExit','-NoProfile','-ExecutionPolicy','Bypass','-Command',$BackendCommand

Write-Host 'Starting frontend on http://127.0.0.1:5173 ...'
Start-Process powershell.exe -ArgumentList '-NoExit','-NoProfile','-ExecutionPolicy','Bypass','-Command',$FrontendCommand

Write-Host 'Waiting for the app to come online...'
$Url = 'http://127.0.0.1:5173'
$Deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $Deadline) {
    try {
        $Response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
        if ($Response.StatusCode -ge 200 -and $Response.StatusCode -lt 500) { break }
    } catch {
        Start-Sleep -Milliseconds 750
    }
}

Start-Process $Url
Write-Host ''
Write-Host 'Paralegal Research Desk is starting.'
Write-Host 'Keep the Backend and Frontend PowerShell windows open while testing.'
Write-Host 'Browser: http://127.0.0.1:5173'
