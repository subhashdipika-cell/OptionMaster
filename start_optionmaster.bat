@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "BACKEND_PORT=8300"
set "FRONTEND_PORT=5275"
set "LAUNCH_LOG_DIR=%CD%\work\launcher-logs"
if not exist "%LAUNCH_LOG_DIR%" mkdir "%LAUNCH_LOG_DIR%"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
) else if exist "C:\Users\Subhash\AppData\Local\Python\bin\python.exe" (
    rem Local Python installed for this machine; it is not registered on PATH.
    set "PYTHON_EXE=C:\Users\Subhash\AppData\Local\Python\bin\python.exe"
) else (
    for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
)

if not defined PYTHON_EXE (
    echo.
    echo Python was not found. Install Python 3.11+ or create .venv first.
    if /i not "%TRADING_LAB_HIDDEN%"=="1" pause
    exit /b 1
)

netstat -ano | findstr /R /C:":%BACKEND_PORT% .*LISTENING" >nul
if errorlevel 1 (
    echo Starting OptionMaster backend on port %BACKEND_PORT%...
    if /i "%TRADING_LAB_HIDDEN%"=="1" (
        start "" /b "%PYTHON_EXE%" -m uvicorn optionmaster.main:app --app-dir backend --host 127.0.0.1 --port %BACKEND_PORT% 1^>^>"%LAUNCH_LOG_DIR%\backend.log" 2^>^&1
    ) else (
        start "OptionMaster Backend" "%PYTHON_EXE%" -m uvicorn optionmaster.main:app --app-dir backend --host 127.0.0.1 --port %BACKEND_PORT%
    )
) else (
    powershell.exe -NoLogo -NoProfile -Command "try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:%BACKEND_PORT%/health' -TimeoutSec 2; if ($r.application -eq 'OptionMaster') { exit 0 } } catch {}; exit 1" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Port %BACKEND_PORT% is occupied by a service that is not OptionMaster.
        exit /b 1
    )
    echo OptionMaster backend is already healthy on port %BACKEND_PORT%.
)

netstat -ano | findstr /R /C:":%FRONTEND_PORT% .*LISTENING" >nul
if errorlevel 1 (
    echo Starting OptionMaster dashboard on port %FRONTEND_PORT%...
    if /i "%TRADING_LAB_HIDDEN%"=="1" (
        start "" /b "%PYTHON_EXE%" -m http.server %FRONTEND_PORT% --bind 127.0.0.1 --directory frontend 1^>^>"%LAUNCH_LOG_DIR%\frontend.log" 2^>^&1
    ) else (
        start "OptionMaster Dashboard" "%PYTHON_EXE%" -m http.server %FRONTEND_PORT% --bind 127.0.0.1 --directory frontend
    )
) else (
    powershell.exe -NoLogo -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort %FRONTEND_PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; $cmd=(Get-CimInstance Win32_Process -Filter \"ProcessId=$($c.OwningProcess)\" -ErrorAction SilentlyContinue).CommandLine; if($cmd -match 'OptionMaster.+http[.]server'){exit 0}; exit 1" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Port %FRONTEND_PORT% is occupied by a service that is not OptionMaster.
        exit /b 1
    )
    echo OptionMaster dashboard is already listening on port %FRONTEND_PORT%.
)

powershell.exe -NoLogo -NoProfile -Command "$deadline=(Get-Date).AddSeconds(30); do { try { $r=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%FRONTEND_PORT%/' -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; Start-Sleep -Milliseconds 500 } while ((Get-Date) -lt $deadline); exit 1"
if errorlevel 1 (
    echo [ERROR] OptionMaster dashboard did not become ready within 30 seconds.
    exit /b 1
)

rem Hand the URL to Windows Shell explicitly so a desktop shortcut opens the default browser.
start "" explorer.exe "http://127.0.0.1:%FRONTEND_PORT%/?build=20260901a"

echo.
echo OptionMaster is available at http://127.0.0.1:%FRONTEND_PORT%/?build=20260901a
endlocal
