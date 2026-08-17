@echo off
REM Fetch the latest prices into this addon folder.
REM
REM WoW cannot do this itself: its Lua sandbox has no sockets and no HTTP, so
REM PriceData.lua has to be a real file on disk before the client loads it.
REM This is that file arriving. Nothing else here talks to the internet.
REM
REM No install of any kind. curl.exe ships with Windows 10 1803 and later, and
REM this script lives inside the addon folder, so it already knows where the
REM file goes - there is no path to configure and nothing to point at WoW.
REM
REM Usage:  sync.cmd  [realm-slug]
REM Set REALM below to your own realm and you can just double-click it.
REM /reload in game afterwards, or it will be picked up at the next login.

setlocal
set "BASE=https://zethrel.github.io/wow-craft-margins"
set "REALM=argent-dawn"

if not "%~1"=="" set "REALM=%~1"

set "DEST=%~dp0PriceData.lua"
set "TMP=%~dp0PriceData.lua.part"

where curl.exe >nul 2>&1
if errorlevel 1 (
    echo curl.exe was not found. It ships with Windows 10 1803 and later;
    echo on anything older, download this by hand instead:
    echo     %BASE%/%REALM%/PriceData.lua
    echo and save it as PriceData.lua in this folder.
    exit /b 9
)

echo Fetching %REALM% prices...
REM -f so a 404 is an error rather than a saved error page. Downloaded beside
REM the target and moved into place, so a failed or half-finished transfer
REM leaves yesterday's prices intact rather than a truncated file that the
REM addon would try to read.
curl -fsS --retry 2 --max-time 120 -o "%TMP%" "%BASE%/%REALM%/PriceData.lua"
if errorlevel 1 (
    echo.
    echo Could not fetch prices for realm "%REALM%".
    echo Check the realm slug - it is lower case with hyphens, as in
    echo argent-dawn or twisting-nether - and that it is one of the realms
    echo being published. Your existing PriceData.lua has not been touched.
    if exist "%TMP%" del "%TMP%"
    exit /b 1
)

REM A zero-length file means something went wrong quietly.
for %%F in ("%TMP%") do if %%~zF LSS 1000 (
    echo Downloaded file is too small to be real ^(%%~zF bytes^); keeping the
    echo prices you already had.
    del "%TMP%"
    exit /b 1
)

move /y "%TMP%" "%DEST%" >nul
if errorlevel 1 (
    echo Could not replace PriceData.lua. Close WoW and try again.
    exit /b 1
)

for %%F in ("%DEST%") do echo Updated PriceData.lua  ^(%%~zF bytes, %%~tF^)
echo Type /reload in game to pick it up.
exit /b 0
