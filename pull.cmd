@echo off
REM Download the latest published scan instead of running one.
REM
REM Point Task Scheduler at THIS FILE (not at `cmd.exe /c "..."` - the project
REM path contains an ampersand, which cmd.exe reads as a command separator).
REM
REM Needs no Battle.net credentials and no recipe cache: the scan already ran
REM in GitHub Actions. This writes PriceData.lua into the addon folder named by
REM addon_path, refreshes dashboard.html, and swaps in the price database that
REM pricecheck reads - keeping this machine's inventory rows, which exist
REM nowhere else.
REM
REM Set pull_url in config.json first. /reload in game afterwards.

call "%~dp0run-scan.cmd" pull
exit /b %ERRORLEVEL%
