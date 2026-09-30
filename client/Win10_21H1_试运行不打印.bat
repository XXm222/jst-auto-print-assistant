@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 (
  echo ERROR: cannot enter the extracted application directory.
  pause
  exit /b 2
)

if not exist "%~dp0JSTAutoPrint_Win10_21H1.exe" (
  echo ERROR: JSTAutoPrint_Win10_21H1.exe is missing.
  echo Extract the whole verified delivery ZIP. Do not copy only this BAT or only the EXE.
  pause
  exit /b 10
)
if not exist "%~dp0verify_jst_win10_build.ps1" (
  echo ERROR: build verification script is missing. Extract the whole ZIP again.
  pause
  exit /b 11
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0verify_jst_win10_build.ps1" -StageDir "%~dp0" -AssertNoRunningInstance
if errorlevel 1 (
  echo ERROR: delivery files failed integrity checks. The program will not start.
  pause
  exit /b 12
)

echo IMPORTANT: no-print trial means no paper is printed.
echo After you click Start in the app, it can still really change the carrier and obtain a waybill.
echo Use only one approved acceptance order.
echo.
start "" "%~dp0JSTAutoPrint_Win10_21H1.exe" --no-print
if errorlevel 1 (
  echo ERROR: Windows could not start the application.
  pause
  exit /b 14
)
exit /b 0
