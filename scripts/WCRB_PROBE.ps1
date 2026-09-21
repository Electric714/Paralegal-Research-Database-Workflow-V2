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
    throw 'Project Python environment not found. Run START_HERE.bat once on this branch first.'
}

New-Item -ItemType Directory -Force -Path $BrowserPath | Out-Null
$env:PLAYWRIGHT_BROWSERS_PATH = $BrowserPath

Write-Host 'Preparing project-local Chromium for the WCRB research spike...'
& $VenvPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    throw "Playwright Chromium installation failed with exit code $LASTEXITCODE."
}

$ProbeArgs = @('-m', 'app.research.sources.wcrb_browser', '--name', $Name)
if (-not $Headless) {
    $ProbeArgs += '--headed'
}

Write-Host "Running WCRB Coverage Lookup probe for: $Name"
Push-Location $Backend
try {
    & $VenvPython @ProbeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "WCRB probe exited with code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
