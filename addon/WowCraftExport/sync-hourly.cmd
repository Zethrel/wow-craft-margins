@echo off
REM Run sync.cmd every hour, so prices are current without you thinking about
REM it. Optional - sync.cmd on its own works fine double-clicked before you
REM play, and that is enough for most people.
REM
REM No admin rights needed: this registers a task that runs as you. Remove it
REM again with:  schtasks /delete /tn "wowcraft price sync" /f
REM
REM Usage:  sync-hourly.cmd  [realm-slug]

setlocal
set "REALM=%~1"
set "TASK=wowcraft price sync"

set "CMD=\"%~dp0sync.cmd\""
if not "%REALM%"=="" set "CMD=\"%~dp0sync.cmd\" %REALM%"

REM :35 past the hour. The prices are published at about :23 and the publisher
REM is routinely late, so fetching on the hour would usually collect the
REM previous hour's numbers.
schtasks /create /tn "%TASK%" /tr "%CMD%" /sc hourly /st 00:35 /f >nul
if errorlevel 1 (
    echo Could not register the task. You can still run sync.cmd by hand.
    exit /b 1
)

echo Registered "%TASK%" - runs at 35 past every hour.
echo Running it once now...
echo.
call "%~dp0sync.cmd" %REALM%
exit /b %ERRORLEVEL%
