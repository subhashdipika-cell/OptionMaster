@echo off
setlocal EnableExtensions
title Close OptionMaster

powershell.exe -NoLogo -NoProfile -Command ^
  "$targets=New-Object 'System.Collections.Generic.HashSet[int]';" ^
  "$rules=@(@{Port=8300;Pattern='optionmaster[.]main:app'},@{Port=5275;Pattern='OptionMaster.+http[.]server'});" ^
  "foreach($rule in $rules){$listener=Get-NetTCPConnection -LocalPort $rule.Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if(-not $listener){continue}; $process=Get-CimInstance Win32_Process -Filter \"ProcessId=$($listener.OwningProcess)\" -ErrorAction SilentlyContinue; if($process.CommandLine -match $rule.Pattern){[void]$targets.Add([int]$process.ProcessId)}else{Write-Warning \"Port $($rule.Port) belongs to another application; it was not stopped.\"}};" ^
  "if($targets.Count -eq 0){Write-Host 'OptionMaster is not running.'; exit 0};" ^
  "foreach($processId in $targets){Write-Host \"Stopping OptionMaster PID $processId...\"; & taskkill.exe /PID $processId /T /F | Out-Null; if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}}; exit 0"

set "CLOSE_EXIT=%ERRORLEVEL%"
if not "%CLOSE_EXIT%"=="0" echo [ERROR] OptionMaster could not be stopped cleanly.
if /i not "%TRADING_LAB_HIDDEN%"=="1" timeout /t 2 /nobreak >nul
endlocal & exit /b %CLOSE_EXIT%
