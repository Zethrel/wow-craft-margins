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
REM The result lands in dist\pricecheck.exe. It reads wowcraft.sqlite3 from
REM its own folder, so keep the two together - or pass --db.

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
echo Built dist\pricecheck.exe
echo Copy it next to wowcraft.sqlite3, or run it with --db ^<path^>.
exit /b 0

:try
if defined PY exit /b 0
if "%~1"=="" exit /b 0
"%~1" -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0
