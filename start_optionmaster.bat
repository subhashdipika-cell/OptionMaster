@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "BACKEND_PORT=8300"
set "FRONTEND_PORT=5275"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_CMD="%CD%\.venv\Scripts\python.exe""
) else (
    where python >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_CMD=python"
    ) else (
        where py >nul 2>nul
        if not errorlevel 1 (
            set "PYTHON_CMD=py -3"
        ) else (
            echo.
            echo Python was not found. Install Python 3.11+ or create .venv first.
            pause
            exit /b 1
        )
    )
)

netstat -ano | findstr /R /C:":%BACKEND_PORT% .*LISTENING" >nul
if errorlevel 1 (
    echo Starting OptionMaster backend on port %BACKEND_PORT%...
    start "OptionMaster Backend" cmd /k call %PYTHON_CMD% -m uvicorn optionmaster.main:app --app-dir backend --host 127.0.0.1 --port %BACKEND_PORT%
) else (
    echo OptionMaster backend is already listening on port %BACKEND_PORT%.
)

netstat -ano | findstr /R /C:":%FRONTEND_PORT% .*LISTENING" >nul
if errorlevel 1 (
    echo Starting OptionMaster dashboard on port %FRONTEND_PORT%...
    start "OptionMaster Dashboard" cmd /k call %PYTHON_CMD% -m http.server %FRONTEND_PORT% --bind 127.0.0.1 --directory frontend
) else (
    echo OptionMaster dashboard is already listening on port %FRONTEND_PORT%.
)

timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:%FRONTEND_PORT%"

echo.
echo OptionMaster is available at http://127.0.0.1:%FRONTEND_PORT%
endlocal
