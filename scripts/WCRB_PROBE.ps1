param(
    [Parameter(Mandatory = $true)]
    [string]$Name,
    [switch]$Headless
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
$BrowserPath = Join-Path $Root '.runtime\ms-playwright'
$Backend = Join-Path $Root 'backend'

if (-not (Test-Path $VenvPython)) {
    throw 'Project Python environment not found. Run START_HERE.bat once before running the WCRB probe.'
}

$env:PYTHONUTF8 = '1'
New-Item -ItemType Directory -Force -Path $BrowserPath | Out-Null
$env:PLAYWRIGHT_BROWSERS_PATH = $BrowserPath

# Fail with a clear instruction if the project environment predates the Playwright
# dependency instead of surfacing a confusing "No module named playwright" error.
& $VenvPython -c "import playwright" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw 'Playwright is not installed in the project environment. Run START_HERE.bat once to refresh backend dependencies.'
}

Write-Host 'Preparing project-local Chromium for WCRB...'
& $VenvPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    throw "Playwright Chromium installation failed with exit code $LASTEXITCODE. Check internet/proxy access and retry."
}

$ProbeArgs = @('-m', 'app.research.sources.wcrb_browser', '--name', $Name)
if (-not $Headless) {
    $ProbeArgs += '--headed'
}

Write-Host "Running WCRB Coverage Lookup probe for: $Name"
Push-Location $Backend
try {
    & $VenvPython @ProbeArgs
    $ProbeExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

if ($ProbeExitCode -ne 0) {
    Write-Host "WCRB probe did not complete successfully (exit code $ProbeExitCode)." -ForegroundColor Yellow
    Write-Host 'If Chromium reached WCRB, the Python probe prints the folder containing diagnostics and captured failure artifacts.' -ForegroundColor Yellow
    exit $ProbeExitCode
}

Write-Host 'WCRB probe completed and saved its capture artifacts.' -ForegroundColor Green
