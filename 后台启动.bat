@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal
title NTE Assistant - background launcher

rem Loopback must never go through a system proxy.
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"
set "PORT=8765"
set "URL=http://127.0.0.1:%PORT%/"
set "NTE_PORT=%PORT%"

rem ---- Locate Python via shared helper (installed Python on any drive > cache > PATH) ----
call "%~dp0find_python.cmd" --print
if errorlevel 1 (
  echo.
  pause
  exit /b 1
)

rem ---- Already running? Just open the page ----
"%NTE_PYTHON%" -c "import socket,sys;s=socket.socket();s.settimeout(0.3);r=s.connect_ex(('127.0.0.1',%PORT%));s.close();sys.exit(0 if r==0 else 1)" >nul 2>&1
if not errorlevel 1 (
  echo Service is already running on port %PORT%.
  echo Opening %URL%
  if not "%NTE_NO_OPEN%"=="1" start "" "%URL%"
  ping -n 3 127.0.0.1 >nul
  exit /b 0
)

rem ---- Launch a separate minimized process that survives this window ----
rem run_service.cmd resolves Python itself and keeps the window open on failure.
echo Starting NTE Assistant in a minimized window...
start "NTE Assistant - running (close this window to stop)" /MIN cmd /c "run_service.cmd"

rem ---- Wait for the listener, then open the page ----
set /a TRIES=0
:WAIT
"%NTE_PYTHON%" -c "import socket,sys;s=socket.socket();s.settimeout(0.3);r=s.connect_ex(('127.0.0.1',%PORT%));s.close();sys.exit(0 if r==0 else 1)" >nul 2>&1
if not errorlevel 1 goto READY
set /a TRIES+=1
if %TRIES% GEQ 20 goto FAILED
ping -n 2 127.0.0.1 >nul
goto WAIT

:FAILED
echo.
echo [ERROR] Service did not come up on port %PORT% within 20 seconds.
echo Check the minimized window and data\run.log for the error message.
echo.
pause
exit /b 1

:READY
echo.
echo ==========================================================
echo   NTE Assistant is running: %URL%
echo   A minimized console window holds the service.
echo   To stop it: close that minimized window from the taskbar.
echo   This launcher window can be closed safely.
echo   Write actions and automation are OFF by default.
echo ==========================================================
echo.
if not "%NTE_NO_OPEN%"=="1" start "" "%URL%"
ping -n 3 127.0.0.1 >nul
endlocal
exit /b 0
