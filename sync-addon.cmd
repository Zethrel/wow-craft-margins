@echo off
REM Copy the addon from this repo into the WoW AddOns folder.
REM
REM The installed copy is the one the game runs, so it is the tempting one to
REM edit - and a plain copy in this direction would silently destroy those
REM edits. So: if the installed file differs AND is newer than the repo's,
REM this refuses to touch it and tells you, rather than overwriting work that
REM was never committed. That is the whole point of the exercise.
REM
REM Usage:  sync-addon.cmd          install repo -> game
REM         sync-addon.cmd back     bring game edits back into the repo

setlocal enabledelayedexpansion

set "SRC=%~dp0addon\WowCraftExport"
set "DST=D:\Games\World of Warcraft\_retail_\Interface\AddOns\WowCraftExport"

if not exist "%DST%" (
    echo Addon folder not found: "%DST%"
    echo Edit DST in this script if WoW lives somewhere else.
    exit /b 1
)

set "MODE=install"
if /i "%~1"=="back" set "MODE=back"

set CHANGED=0
set BLOCKED=0

for %%F in (WowCraftExport.toc main.lua prices.lua) do (
    set "A=%SRC%\%%F"
    set "B=%DST%\%%F"
    if not exist "!A!" (
        echo missing from repo : %%F
    ) else (
        fc /b "!A!" "!B!" >nul 2>&1
        if errorlevel 1 (
            if "%MODE%"=="back" (
                copy /y "!B!" "!A!" >nul && echo pulled back      : %%F
                set /a CHANGED+=1
            ) else (
                REM Which side is newer? %%~tF style comparison is awkward in
                REM batch, so ask PowerShell for a straight answer.
                for /f %%N in ('powershell -NoProfile -Command ^
                    "if ((Get-Item '!B!' -ErrorAction SilentlyContinue).LastWriteTime -gt (Get-Item '!A!').LastWriteTime) {'game'} else {'repo'}"') do set "NEWER=%%N"
                if "!NEWER!"=="game" (
                    echo REFUSED           : %%F
                    echo     the installed copy differs and is NEWER than the repo's.
                    echo     Run "sync-addon.cmd back" to keep those edits, or delete
                    echo     the installed file if you meant to discard them.
                    set /a BLOCKED+=1
                ) else (
                    copy /y "!A!" "!B!" >nul && echo installed         : %%F
                    set /a CHANGED+=1
                )
            )
        ) else (
            echo already in sync   : %%F
        )
    )
)

echo.
if "%MODE%"=="back" (
    echo %CHANGED% file^(s^) pulled back into the repo. Commit them.
) else (
    echo %CHANGED% file^(s^) installed, %BLOCKED% refused.
)
if not "%BLOCKED%"=="0" exit /b 2
exit /b 0
