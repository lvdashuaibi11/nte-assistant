@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal
title NTE Assistant - local service

rem Loopback must never go through a system proxy.
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"
set "PORT=8765"
set "URL=http://127.0.0.1:%PORT%/"

rem ---- Locate Python via shared helper (installed Python on any drive > cache > PATH) ----
call "%~dp0find_python.cmd" --print
if errorlevel 1 (
  echo.
  pause
  exit /b 1
)

rem ---- Already running? Open the page instead of starting a 2nd instance ----
"%NTE_PYTHON%" -c "import socket,sys;s=socket.socket();s.settimeout(0.3);r=s.connect_ex(('127.0.0.1',%PORT%));s.close();sys.exit(0 if r==0 else 1)" >nul 2>&1
if not errorlevel 1 (
  echo Service is already running on port %PORT%.
  echo Opening %URL%
  if not "%NTE_NO_OPEN%"=="1" start "" "%URL%"
  ping -n 3 127.0.0.1 >nul
  exit /b 0
)

echo.
echo ==========================================================
echo   NTE Assistant  -  local web service
echo   URL : %URL%
echo   KEEP THIS WINDOW OPEN. Press Ctrl+C to stop.
echo   Write actions and automation are OFF by default.
echo ==========================================================
echo.

set "NTE_PORT=%PORT%"
"%NTE_PYTHON%" app.py
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
  echo [ERROR] Service exited with code %RC%.
  echo Possible causes: port %PORT% is taken, or app.py failed to load.
) else (
  echo Service stopped normally.
)
echo.
pause
endlocal
