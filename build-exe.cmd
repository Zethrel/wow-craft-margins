@echo off
REM Build pricecheck.exe.
REM
REM PyInstaller is the only third-party dependency in the project, and it is a
REM BUILD dependency only - nothing that runs needs it, and the scanner still
REM has none. Install it with:  python -m pip install pyinstaller
REM
REM --onefile      one .exe, nothing to install
REM --windowed     no console window behind the app
REM --clean        do not reuse a stale build cache after a code change
REM
REM PyInstaller writes dist\pricecheck.exe, and this then copies it up beside
REM wowcraft.sqlite3 - which is where you actually launch it from, and where
REM it needs to be to find the database at all. That copy used to be a line of
REM advice at the end instead of a step, so a rebuild appeared to do nothing:
REM the build succeeded every time and the exe being double-clicked was a
REM different, older file.

setlocal
cd /d "%~dp0"

set "PY="
call :try "%WOWCRAFT_PYTHON%"
call :try "C:\Program Files\Python313\python.exe"
call :try "C:\Program Files\Python312\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
call :try "python"

if not defined PY (
    echo No usable Python found.
    exit /b 9
)

"%PY%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo PyInstaller is not installed. Run:
    echo     "%PY%" -m pip install pyinstaller
    exit /b 9
)

echo Building with %PY% ...
"%PY%" -m PyInstaller --onefile --windowed --clean ^
    --name pricecheck ^
    --distpath dist --workpath build --specpath build ^
    pricecheck.py
if errorlevel 1 (
    echo.
    echo BUILD FAILED
    exit /b 1
)

echo.

REM Windows will not overwrite a running executable, and the whole point of
REM this tool is that you leave it open. Say so plainly rather than failing
REM with "access is denied" and leaving a stale exe behind a fresh build.
copy /y "dist\pricecheck.exe" "pricecheck.exe" >nul
if errorlevel 1 (
    echo Built dist\pricecheck.exe, but could not replace pricecheck.exe.
    echo Close the price lookup window if it is open, then run this again.
    echo Nothing is lost - the new build is in dist\.
    exit /b 1
)

for %%F in (pricecheck.exe) do echo Built pricecheck.exe  ^(%%~zF bytes, %%~tF^)
echo Run it with pricecheck.cmd, or double-click pricecheck.exe.
exit /b 0

:try
if defined PY exit /b 0
if "%~1"=="" exit /b 0
"%~1" -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0
