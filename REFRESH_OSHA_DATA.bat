@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo.
    echo The project Python environment is not installed yet.
    echo Run START_HERE.bat once, close the app if desired, then run this file again.
    echo.
    pause
    exit /b 1
)

echo.
echo Refreshing the official U.S. Department of Labor OSHA inspection dataset...
echo The first/current dataset download is large. Do not close this window until it finishes.
echo.
"%PYTHON%" scripts\refresh_osha_bulk.py
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo OSHA data refresh completed successfully.
) else (
    echo OSHA data refresh failed with exit code %RC%.
)
echo.
pause
exit /b %RC%
