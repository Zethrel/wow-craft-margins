@echo off
REM Hourly scan wrapper for Task Scheduler.
REM
REM Takes the wowcraft subcommand as its first argument and defaults to `scan`,
REM so an existing scheduled task that just points here keeps working. pull.cmd
REM passes `pull` instead, which is the same plumbing - find a Python, log with
REM a timestamp, trim the log - around a command that needs no credentials.
REM
REM A wrapper rather than putting the command straight into the task: the
REM project path contains both spaces and an ampersand, and cmd.exe treats the
REM ampersand as a command separator. Point the task's Execute at THIS FILE,
REM not at `cmd.exe /c "...run-scan.cmd"` - the latter dies before its first
REM line with exit code 1 and an empty log.
REM
REM Blizzard refreshes auction data hourly and snapshots are keyed on the
REM server's own Last-Modified, so running more often is harmless: it
REM overwrites the same snapshot rather than inventing new history.

setlocal enabledelayedexpansion

set "PROJECT=%~dp0"
cd /d "%PROJECT%"

set "WCCMD=%~1"
if "%WCCMD%"=="" set "WCCMD=scan"

REM -- find an interpreter ---------------------------------------------------
REM This machine runs Python from the Microsoft Store, whose real executable
REM under C:\Program Files\WindowsApps refuses to run (ACL: access denied).
REM The only working route is the App Execution Alias in WindowsApps, and
REM those are unreliable outside an interactive logon - which is precisely the
REM case for a task set to "run whether user is logged on or not". So try
REM every candidate and say which one worked, rather than failing silently at
REM 3am with an empty log.

REM Candidates are tested by RUNNING them, never by Test-Path alone: the
REM registry points at the Store's real executable, which exists and then
REM refuses to launch, so an existence check picks the one path guaranteed to
REM fail and never reaches the alias that works.

echo.>> scan.log
echo ==== %DATE% %TIME% (%WCCMD%) ==== >> scan.log

set "PY="
REM Real installs first. The py.exe launcher comes after them because it can
REM resolve to the Store build, which is the one that will not run in a
REM non-interactive session - the whole problem this ordering avoids.
call :try "%WOWCRAFT_PYTHON%"
call :try "C:\Program Files\Python313\python.exe"
call :try "C:\Program Files\Python312\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
call :try "C:\Python313\python.exe"
call :try "C:\Windows\py.exe"
call :try "%LOCALAPPDATA%\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe"
call :try "python"
for /f "tokens=2,*" %%A in (
  'reg query "HKCU\SOFTWARE\Python\PythonCore\3.13\InstallPath" /ve 2^>nul ^| findstr REG_SZ'
) do call :try "%%B\python.exe"

if not defined PY (
  echo ---- FATAL: no usable Python found. >> scan.log
  echo ---- This machine runs Python from the Microsoft Store: its real exe >> scan.log
  echo ---- is ACL-blocked and only the App Execution Alias runs, which does >> scan.log
  echo ---- not resolve outside an interactive logon. Either install >> scan.log
  echo ---- python.org Python, or set WOWCRAFT_PYTHON to a working >> scan.log
  echo ---- python.exe, or run this task only while logged on. >> scan.log
  exit /b 9
)
echo ---- python: %PY% >> scan.log

"%PY%" wowcraft.py %WCCMD% >> scan.log 2>&1
set RC=%ERRORLEVEL%
if not "%RC%"=="0" echo ---- exited with code %RC% >> scan.log

REM Keep the log from growing without bound: past ~2 MB, keep the tail.
for %%F in (scan.log) do if %%~zF GTR 2000000 (
  powershell -NoProfile -Command "Get-Content scan.log -Tail 400 | Set-Content scan.log.tmp -Encoding utf8"
  move /y scan.log.tmp scan.log >nul
)

exit /b %RC%

:try
REM Accept the first candidate that actually starts. Anything that is missing,
REM ACL-blocked, or an alias that will not resolve simply fails here.
if defined PY exit /b 0
if "%~1"=="" exit /b 0
"%~1" -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0
