@echo off
chcp 65001 >nul 2>&1
rem Shared locator for a usable Python 3.10+ interpreter.
rem Sets NTE_PYTHON (and NTE_PYTHON_SOURCE) in the caller's scope.
rem Priority:
rem   1) explicit NTE_PYTHON override (or inherited from the parent launcher)
rem   2) runtime shipped inside this folder (portable build: runtime\python.exe)
rem   3) user-installed Python on ANY local drive, e.g. F:\Python312\python.exe
rem   4) cached path in data\python_path.txt
rem   5) py -3 launcher
rem   6) real python.exe on PATH (Microsoft Store alias is filtered out)
rem   7) standard install folders
rem   8) WorkBuddy bundled Python (last resort; may disappear on upgrade)
rem Usage: call "%~dp0find_python.cmd" [--print] [--rescan]
setlocal
set "CACHE=%~dp0data\python_path.txt"
set "FOUND="
set "FOUND_SOURCE="
set "DOPRINT="
for %%A in (%*) do (
  if /i "%%A"=="--print" set "DOPRINT=1"
  if /i "%%A"=="--rescan" (
    if exist "%CACHE%" del "%CACHE%" >nul 2>&1
    set "NTE_PYTHON="
  )
)

rem 1) an explicitly chosen interpreter always wins
set "SRC=NTE_PYTHON override"
if defined NTE_PYTHON call :TRY "%NTE_PYTHON%"
if defined FOUND goto DONE

rem 1b) runtime shipped inside this folder (portable / no-install build).
rem     If present it beats anything installed on the machine: the receiver
rem     does not need Python at all, and we never touch their PATH or registry.
set "SRC=bundled runtime inside this folder"
if exist "%~dp0runtime\python.exe" call :TRY "%~dp0runtime\python.exe"
if defined FOUND goto DONE

rem 2) installations outside C: that are not in PATH and not in the registry
rem    (this machine: F:\Python312, Python 3.12.10)
set "SRC=installed Python on a local drive"
for %%L in (C D E F G H I J K L M N O P Q R S T U V W X Y Z) do call :SCAN_DRIVE %%L
if defined FOUND goto DONE

rem 3) path remembered from a previous successful launch
set "SRC=cached path from data\python_path.txt"
if exist "%CACHE%" (
  for /f "usebackq delims=" %%A in ("%CACHE%") do call :TRY "%%A"
)
if defined FOUND goto DONE

rem 4) official Python launcher: resolve the real interpreter behind py.exe
set "SRC=py -3 launcher"
for /f "delims=" %%A in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do call :TRY "%%A"
if defined FOUND goto DONE

rem 5) any real python.exe on PATH (Microsoft Store alias is filtered out)
set "SRC=PATH"
for /f "delims=" %%A in ('where python 2^>nul') do call :TRY "%%A"
if defined FOUND goto DONE

rem 6) standard install directories
set "SRC=standard install folder"
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :TRY "%%~D\python.exe"
if defined FOUND goto DONE
for /d %%D in ("%ProgramFiles%\Python3*") do call :TRY "%%~D\python.exe"
if defined FOUND goto DONE
for /d %%D in ("C:\Python3*") do call :TRY "%%~D\python.exe"
if defined FOUND goto DONE

rem 7) WorkBuddy bundled Python (deliberately not added to PATH)
set "SRC=WorkBuddy bundled Python as fallback"
for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do call :TRY "%%~D\python.exe"
if defined FOUND goto DONE

echo [ERROR] No usable Python 3.10+ was found on this machine.
echo Searched: NTE_PYTHON override, all local drives (X:\Python3*, X:\Python,
echo X:\Anaconda3, X:\Miniconda3, X:\Program Files\Python3*), cached path,
echo py -3, PATH, standard install folders, and WorkBuddy bundled Python.
echo If Python lives in an unusual folder, set NTE_PYTHON before launching:
echo   set "NTE_PYTHON=D:\my\folder\python.exe"
endlocal & set "NTE_PYTHON=" & exit /b 1

:DONE
if not exist "%~dp0data" mkdir "%~dp0data" >nul 2>&1
>"%CACHE%" echo %FOUND%
if defined DOPRINT (
  echo Python: %FOUND%
  echo Source: %FOUND_SOURCE%
  "%FOUND%" --version
)
endlocal & set "NTE_PYTHON=%FOUND%" & set "NTE_PYTHON_SOURCE=%FOUND_SOURCE%"
exit /b 0

rem ---- Scan one drive letter for common "installed but not on PATH" layouts ----
:SCAN_DRIVE
if defined FOUND goto :eof
if not exist "%~1:\" goto :eof
for /d %%P in ("%~1:\Python3*") do call :TRY "%%~P\python.exe"
if defined FOUND goto :eof
if exist "%~1:\Python\python.exe" call :TRY "%~1:\Python\python.exe"
if defined FOUND goto :eof
if exist "%~1:\Anaconda3\python.exe" call :TRY "%~1:\Anaconda3\python.exe"
if defined FOUND goto :eof
if exist "%~1:\Miniconda3\python.exe" call :TRY "%~1:\Miniconda3\python.exe"
if defined FOUND goto :eof
for /d %%P in ("%~1:\Program Files\Python3*") do call :TRY "%%~P\python.exe"
if defined FOUND goto :eof
for /d %%P in ("%~1:\Program Files (x86)\Python3*") do call :TRY "%%~P\python.exe"
if defined FOUND goto :eof
for /d %%P in ("%~1:\Tools\Python3*") do call :TRY "%%~P\python.exe"
goto :eof

:TRY
if defined FOUND goto :eof
call :CHECK "%~1"
goto :eof

:CHECK
if "%~1"=="" goto :eof
if not exist "%~1" goto :eof
set "P=%~1"
if not "%P:WindowsApps=%"=="%P%" goto :eof
"%~1" -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 goto :eof
set "FOUND=%~1"
set "FOUND_SOURCE=%SRC%"
goto :eof
