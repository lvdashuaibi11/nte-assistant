@echo off
chcp 65001 >nul 2>&1
rem Long-running service body, started minimized by the background launcher bat.
rem Kept separate so a startup failure stays visible instead of closing the window.
cd /d "%~dp0"
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"
if not defined NTE_PORT set "NTE_PORT=8765"
set "NTE_NO_BROWSER=1"
title NTE Assistant - running (close this window to stop)

if /i "%~1"=="--check" (
  call "%~dp0find_python.cmd" --print
  if errorlevel 1 exit /b 1
  echo [OK] Service launcher is ready; no server was started.
  exit /b 0
)

call "%~dp0find_python.cmd" --print
if errorlevel 1 (
  echo.
  pause
  exit /b 1
)
echo.
echo Starting: "%NTE_PYTHON%" app.py
echo Keep this window minimized. Closing it stops the service.
echo.
"%NTE_PYTHON%" app.py
set "RC=%ERRORLEVEL%"
echo.
echo [ERROR] Service exited with code %RC%.
echo Check data\run.log for details. This window stays open so the error is readable.
echo.
pause
